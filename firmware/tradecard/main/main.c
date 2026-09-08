/*
 * TradeCard firmware — ESP32-S3 skeleton.
 *
 * Hardware profile (per product spec):
 *   - Nokia-5110-class B&W pixel LCD (PCD8544, 84x48). SPI + D/C + RST + CE.
 *   - Five-key input: UP / DOWN / LEFT / RIGHT / CENTER (active-low, pull-ups).
 *     UP + DOWN scroll passbook / long-press to switch mode.
 *     LEFT = DECLINE, RIGHT = ACCEPT, CENTER = open detail.
 *   - Persistent "passbook" — a ring buffer of the last N intents + verdicts,
 *     browsable when no live prompt is on screen.
 *   - Broker selection: prompts carry `broker` = "ib" | "kraken" | "questrade".
 *     Canadian users typically pair with IB + Kraken; the card displays and
 *     signs the broker so a MITM cannot silently redirect the order.
 *
 * Loop:
 *   1. Bring up Wi-Fi from NVS-stored SSID/PSK.
 *   2. Load Ed25519 keypair from NVS; on first boot, generate + register.
 *   3. Long-poll GET /intents/pending every POLL_INTERVAL_MS.
 *   4. On a new prompt: enter MODE_PROMPT, render (broker, action, symbol,
 *      shares, $notional, thesis), wait up to TTL for RIGHT/LEFT/CENTER.
 *   5. Sign the prompt's `canonical` bytes with Ed25519 (libsodium).
 *   6. POST base64(sig) + decision, append to passbook.
 *   7. When idle, MODE_PASSBOOK lets the user scroll history with UP/DOWN.
 *
 * Not production. TODO markers below flag the parts you must implement.
 */

#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_system.h"

#include "nvs_flash.h"
#include "nvs.h"

#include "driver/gpio.h"
#include "driver/spi_master.h"

#include "cJSON.h"

#include "sodium.h"

/* --------------------------------------------------------------------- */
/* config — overrides in menuconfig                                      */
/* --------------------------------------------------------------------- */

#define WIFI_SSID           CONFIG_TRADECARD_WIFI_SSID
#define WIFI_PSK            CONFIG_TRADECARD_WIFI_PSK
#define SHIM_URL_BASE       CONFIG_TRADECARD_SHIM_URL
#define CARD_ID             CONFIG_TRADECARD_CARD_ID

#define POLL_INTERVAL_MS    1500
#define HTTP_RX_BUF         4096
#define PASSBOOK_ENTRIES    32
#define PASSBOOK_LINE_LEN   40

/* GPIO — 5-key D-pad + center */
#define GPIO_UP             GPIO_NUM_4
#define GPIO_DOWN           GPIO_NUM_5
#define GPIO_LEFT           GPIO_NUM_6
#define GPIO_RIGHT          GPIO_NUM_7
#define GPIO_CENTER         GPIO_NUM_8

/* PCD8544 LCD wiring (Nokia-5110 class). Adjust to your board. */
#define LCD_MOSI            GPIO_NUM_11
#define LCD_SCLK            GPIO_NUM_12
#define LCD_CE              GPIO_NUM_10
#define LCD_DC              GPIO_NUM_9
#define LCD_RST             GPIO_NUM_13
#define LCD_BL              GPIO_NUM_14

#define LCD_WIDTH           84
#define LCD_HEIGHT          48

/* NVS keys ------------------------------------------------------------ */
#define NVS_NS              "tradecard"
#define NVS_KEY_SK          "ed25519_sk"
#define NVS_KEY_PK          "ed25519_pk"
#define NVS_KEY_REGISTERED  "registered"
#define NVS_KEY_PASSBOOK    "passbook"

static const char *TAG = "tradecard";

static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED_BIT BIT0

static uint8_t g_sk[crypto_sign_SECRETKEYBYTES];
static uint8_t g_pk[crypto_sign_PUBLICKEYBYTES];

/* --------------------------------------------------------------------- */
/* passbook — persistent ring buffer of recent verdicts                  */
/* --------------------------------------------------------------------- */

typedef struct __attribute__((packed)) {
    int64_t  ts_epoch;                 /* seconds since epoch */
    char     verdict;                  /* 'A' | 'D' | 'X' (expired) */
    char     broker[8];                /* "ib", "kraken", "questrade" */
    char     line[PASSBOOK_LINE_LEN];  /* e.g. "BUY XIC.TO 12sh $372" */
} pb_entry_t;

typedef struct __attribute__((packed)) {
    uint16_t   head;
    uint16_t   count;
    pb_entry_t entries[PASSBOOK_ENTRIES];
} passbook_t;

static passbook_t g_pb;

static void passbook_load(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        memset(&g_pb, 0, sizeof(g_pb));
        return;
    }
    size_t sz = sizeof(g_pb);
    esp_err_t e = nvs_get_blob(h, NVS_KEY_PASSBOOK, &g_pb, &sz);
    nvs_close(h);
    if (e != ESP_OK || sz != sizeof(g_pb)) memset(&g_pb, 0, sizeof(g_pb));
}

static void passbook_save(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_blob(h, NVS_KEY_PASSBOOK, &g_pb, sizeof(g_pb));
    nvs_commit(h);
    nvs_close(h);
}

static void passbook_append(char verdict, const char *broker, const char *line) {
    pb_entry_t *e = &g_pb.entries[g_pb.head];
    e->ts_epoch = (int64_t)(esp_timer_get_time() / 1000000);
    e->verdict = verdict;
    strncpy(e->broker, broker ? broker : "?", sizeof(e->broker) - 1);
    e->broker[sizeof(e->broker) - 1] = '\0';
    strncpy(e->line, line, PASSBOOK_LINE_LEN - 1);
    e->line[PASSBOOK_LINE_LEN - 1] = '\0';
    g_pb.head = (g_pb.head + 1) % PASSBOOK_ENTRIES;
    if (g_pb.count < PASSBOOK_ENTRIES) g_pb.count++;
    passbook_save();
}

/* Iterate newest-first. index=0 is the most recent entry. */
static const pb_entry_t *passbook_at(uint16_t index) {
    if (index >= g_pb.count) return NULL;
    int i = (int)g_pb.head - 1 - (int)index;
    while (i < 0) i += PASSBOOK_ENTRIES;
    return &g_pb.entries[i];
}

/* --------------------------------------------------------------------- */
/* LCD (PCD8544) — driver skeleton                                       */
/* --------------------------------------------------------------------- */

static spi_device_handle_t s_lcd;

static void lcd_write(uint8_t is_data, const uint8_t *buf, size_t len) {
    gpio_set_level(LCD_DC, is_data);
    spi_transaction_t t = { .length = len * 8, .tx_buffer = buf };
    spi_device_polling_transmit(s_lcd, &t);
}

static void lcd_cmd(uint8_t c) { lcd_write(0, &c, 1); }

static void lcd_init(void) {
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << LCD_DC) | (1ULL << LCD_RST) | (1ULL << LCD_BL),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&io);
    gpio_set_level(LCD_BL, 1);
    gpio_set_level(LCD_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(LCD_RST, 1);

    spi_bus_config_t bus = {
        .mosi_io_num = LCD_MOSI, .miso_io_num = -1, .sclk_io_num = LCD_SCLK,
        .quadwp_io_num = -1, .quadhd_io_num = -1, .max_transfer_sz = 512,
    };
    spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);
    spi_device_interface_config_t dev = {
        .clock_speed_hz = 4 * 1000 * 1000, .mode = 0, .spics_io_num = LCD_CE,
        .queue_size = 4,
    };
    spi_bus_add_device(SPI2_HOST, &dev, &s_lcd);

    /* Extended instruction set: contrast, temp coefficient, bias. */
    lcd_cmd(0x21); lcd_cmd(0xBF); lcd_cmd(0x04); lcd_cmd(0x14);
    /* Basic instruction set, normal display mode. */
    lcd_cmd(0x20); lcd_cmd(0x0C);
}

/* framebuffer: 84 * 48 / 8 = 504 bytes */
static uint8_t s_fb[LCD_WIDTH * LCD_HEIGHT / 8];

static void lcd_clear(void) { memset(s_fb, 0, sizeof(s_fb)); }

static void lcd_flush(void) {
    lcd_cmd(0x40); lcd_cmd(0x80);           /* set y=0, x=0 */
    lcd_write(1, s_fb, sizeof(s_fb));
}

/* TODO: link a real 5x7 font (Adafruit-GFX / u8g2 style) — this is a stub. */
static void lcd_puts(int row, const char *s) {
    (void)row;
    /* Stub — mirror to log so you can iterate on layout before the LCD's up. */
    ESP_LOGI(TAG, "LCD row %d: %s", row, s);
}

/* --------------------------------------------------------------------- */
/* Wi-Fi                                                                 */
/* --------------------------------------------------------------------- */

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_bringup(void) {
    s_wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL);
    wifi_config_t w = {0};
    strncpy((char *)w.sta.ssid, WIFI_SSID, sizeof(w.sta.ssid));
    strncpy((char *)w.sta.password, WIFI_PSK, sizeof(w.sta.password));
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &w);
    esp_wifi_start();
    esp_wifi_connect();
    xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
}

/* --------------------------------------------------------------------- */
/* keys                                                                  */
/* --------------------------------------------------------------------- */

static esp_err_t keys_load_or_create(void) {
    nvs_handle_t h;
    ESP_ERROR_CHECK(nvs_open(NVS_NS, NVS_READWRITE, &h));
    size_t sk_len = sizeof(g_sk), pk_len = sizeof(g_pk);
    esp_err_t sk = nvs_get_blob(h, NVS_KEY_SK, g_sk, &sk_len);
    esp_err_t pk = nvs_get_blob(h, NVS_KEY_PK, g_pk, &pk_len);
    if (sk == ESP_OK && pk == ESP_OK) { nvs_close(h); return ESP_OK; }
    if (crypto_sign_keypair(g_pk, g_sk) != 0) { nvs_close(h); return ESP_FAIL; }
    nvs_set_blob(h, NVS_KEY_SK, g_sk, sizeof(g_sk));
    nvs_set_blob(h, NVS_KEY_PK, g_pk, sizeof(g_pk));
    nvs_set_u8(h, NVS_KEY_REGISTERED, 0);
    nvs_commit(h);
    nvs_close(h);
    return ESP_OK;
}

/* Ed25519 SubjectPublicKeyInfo DER prefix (12 bytes) + 32-byte pubkey. */
static const uint8_t SPKI_PREFIX[] = {
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00
};

static char *pubkey_pem(void) {
    uint8_t spki[sizeof(SPKI_PREFIX) + 32];
    memcpy(spki, SPKI_PREFIX, sizeof(SPKI_PREFIX));
    memcpy(spki + sizeof(SPKI_PREFIX), g_pk, 32);
    size_t b64_len = sodium_base64_ENCODED_LEN(sizeof(spki), sodium_base64_VARIANT_ORIGINAL);
    char *b64 = malloc(b64_len);
    sodium_bin2base64(b64, b64_len, spki, sizeof(spki), sodium_base64_VARIANT_ORIGINAL);
    size_t cap = b64_len + 128;
    char *pem = malloc(cap);
    int off = snprintf(pem, cap, "-----BEGIN PUBLIC KEY-----\n");
    for (size_t i = 0; i < strlen(b64); i += 64) {
        int n = (int)strlen(b64) - (int)i; if (n > 64) n = 64;
        off += snprintf(pem + off, cap - off, "%.*s\n", n, b64 + i);
    }
    snprintf(pem + off, cap - off, "-----END PUBLIC KEY-----\n");
    free(b64);
    return pem;
}

/* --------------------------------------------------------------------- */
/* HTTP                                                                  */
/* --------------------------------------------------------------------- */

typedef struct { char *buf; size_t len; size_t cap; } rx_t;

static esp_err_t http_event(esp_http_client_event_t *evt) {
    rx_t *rx = evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA && rx && evt->data_len > 0) {
        if (rx->len + evt->data_len + 1 > rx->cap) return ESP_OK;
        memcpy(rx->buf + rx->len, evt->data, evt->data_len);
        rx->len += evt->data_len; rx->buf[rx->len] = '\0';
    }
    return ESP_OK;
}

static int http_post_json(const char *url, const char *body, rx_t *rx) {
    esp_http_client_config_t c = {
        .url = url, .method = HTTP_METHOD_POST, .event_handler = http_event,
        .user_data = rx, .timeout_ms = 10000,
    };
    esp_http_client_handle_t h = esp_http_client_init(&c);
    esp_http_client_set_header(h, "Content-Type", "application/json");
    esp_http_client_set_post_field(h, body, strlen(body));
    esp_err_t err = esp_http_client_perform(h);
    int s = (err == ESP_OK) ? esp_http_client_get_status_code(h) : -1;
    esp_http_client_cleanup(h);
    return s;
}

static int http_get(const char *url, rx_t *rx) {
    esp_http_client_config_t c = {
        .url = url, .method = HTTP_METHOD_GET, .event_handler = http_event,
        .user_data = rx, .timeout_ms = 10000,
    };
    esp_http_client_handle_t h = esp_http_client_init(&c);
    esp_err_t err = esp_http_client_perform(h);
    int s = (err == ESP_OK) ? esp_http_client_get_status_code(h) : -1;
    esp_http_client_cleanup(h);
    return s;
}

static void register_if_needed(void) {
    nvs_handle_t h;
    ESP_ERROR_CHECK(nvs_open(NVS_NS, NVS_READWRITE, &h));
    uint8_t reg = 0; nvs_get_u8(h, NVS_KEY_REGISTERED, &reg);
    if (reg) { nvs_close(h); return; }
    char *pem = pubkey_pem();
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "card_id", CARD_ID);
    cJSON_AddStringToObject(root, "pubkey_pem", pem);
    char *body = cJSON_PrintUnformatted(root);
    char url[256]; snprintf(url, sizeof(url), "%s/card/register", SHIM_URL_BASE);
    char rxbuf[512] = {0}; rx_t rx = { .buf = rxbuf, .cap = sizeof(rxbuf) };
    int status = http_post_json(url, body, &rx);
    ESP_LOGI(TAG, "register: %d %s", status, rxbuf);
    free(pem); free(body); cJSON_Delete(root);
    if (status == 200 || status == 201) {
        nvs_set_u8(h, NVS_KEY_REGISTERED, 1); nvs_commit(h);
    }
    nvs_close(h);
}

/* --------------------------------------------------------------------- */
/* 5-key input                                                           */
/* --------------------------------------------------------------------- */

typedef enum { KEY_NONE, KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_CENTER } key_t;

static void gpio_setup(void) {
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << GPIO_UP) | (1ULL << GPIO_DOWN) |
                        (1ULL << GPIO_LEFT) | (1ULL << GPIO_RIGHT) |
                        (1ULL << GPIO_CENTER),
        .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&io);
}

static key_t read_key_nonblock(void) {
    if (gpio_get_level(GPIO_UP)     == 0) return KEY_UP;
    if (gpio_get_level(GPIO_DOWN)   == 0) return KEY_DOWN;
    if (gpio_get_level(GPIO_LEFT)   == 0) return KEY_LEFT;
    if (gpio_get_level(GPIO_RIGHT)  == 0) return KEY_RIGHT;
    if (gpio_get_level(GPIO_CENTER) == 0) return KEY_CENTER;
    return KEY_NONE;
}

static key_t wait_key(int timeout_ms) {
    int elapsed = 0; const int step = 20;
    while (elapsed < timeout_ms) {
        key_t k = read_key_nonblock();
        if (k != KEY_NONE) {
            vTaskDelay(pdMS_TO_TICKS(30));  /* debounce */
            while (read_key_nonblock() != KEY_NONE) vTaskDelay(pdMS_TO_TICKS(20));
            return k;
        }
        vTaskDelay(pdMS_TO_TICKS(step));
        elapsed += step;
    }
    return KEY_NONE;
}

/* --------------------------------------------------------------------- */
/* rendering                                                             */
/* --------------------------------------------------------------------- */

static void render_prompt(cJSON *p) {
    const char *broker = cJSON_GetStringValue(cJSON_GetObjectItem(p, "broker"));
    const char *action = cJSON_GetStringValue(cJSON_GetObjectItem(p, "action"));
    const char *symbol = cJSON_GetStringValue(cJSON_GetObjectItem(p, "symbol"));
    const char *thesis = cJSON_GetStringValue(cJSON_GetObjectItem(p, "thesis"));
    int shares = cJSON_GetObjectItem(p, "shares")->valueint;
    double notional = cJSON_GetObjectItem(p, "notional_usd")->valuedouble;

    char l1[24], l2[24], l3[24], l4[24];
    snprintf(l1, sizeof(l1), "[%s]", broker ? broker : "?");
    snprintf(l2, sizeof(l2), "%s %s", action ? action : "?", symbol ? symbol : "?");
    snprintf(l3, sizeof(l3), "%d sh $%.0f", shares, notional);
    snprintf(l4, sizeof(l4), "%.20s", thesis ? thesis : "");

    lcd_clear();
    lcd_puts(0, l1);
    lcd_puts(1, l2);
    lcd_puts(2, l3);
    lcd_puts(3, l4);
    lcd_puts(5, "L=DEC R=ACC C=?");
    lcd_flush();
}

static void render_passbook(uint16_t cursor) {
    lcd_clear();
    lcd_puts(0, "== Passbook ==");
    for (int r = 0; r < 4; r++) {
        const pb_entry_t *e = passbook_at(cursor + r);
        if (!e) break;
        char row[32];
        snprintf(row, sizeof(row), "%c %-6.6s %s",
                 e->verdict, e->broker, e->line);
        lcd_puts(r + 1, row);
    }
    lcd_puts(5, "UP/DN scroll");
    lcd_flush();
}

/* --------------------------------------------------------------------- */
/* prompt handling                                                       */
/* --------------------------------------------------------------------- */

static void sign_and_respond(const char *intent_id, const char *canonical,
                             int accepted) {
    uint8_t sig[crypto_sign_BYTES];
    crypto_sign_detached(sig, NULL, (const uint8_t *)canonical,
                         strlen(canonical), g_sk);
    size_t b64_len = sodium_base64_ENCODED_LEN(sizeof(sig), sodium_base64_VARIANT_ORIGINAL);
    char *sig_b64 = malloc(b64_len);
    sodium_bin2base64(sig_b64, b64_len, sig, sizeof(sig), sodium_base64_VARIANT_ORIGINAL);
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "decision", accepted ? "ACCEPT" : "DECLINE");
    cJSON_AddStringToObject(root, "card_id", CARD_ID);
    cJSON_AddStringToObject(root, "signature", sig_b64);
    char *body = cJSON_PrintUnformatted(root);
    char url[320];
    snprintf(url, sizeof(url), "%s/intents/%s/response", SHIM_URL_BASE, intent_id);
    char rxbuf[256] = {0}; rx_t rx = { .buf = rxbuf, .cap = sizeof(rxbuf) };
    int status = http_post_json(url, body, &rx);
    ESP_LOGI(TAG, "respond %s: %d %s", intent_id, status, rxbuf);
    free(sig_b64); free(body); cJSON_Delete(root);
}

static void handle_prompt(cJSON *p) {
    const char *id     = cJSON_GetStringValue(cJSON_GetObjectItem(p, "intent_id"));
    const char *canon  = cJSON_GetStringValue(cJSON_GetObjectItem(p, "canonical"));
    const char *broker = cJSON_GetStringValue(cJSON_GetObjectItem(p, "broker"));
    const char *action = cJSON_GetStringValue(cJSON_GetObjectItem(p, "action"));
    const char *symbol = cJSON_GetStringValue(cJSON_GetObjectItem(p, "symbol"));
    int shares = cJSON_GetObjectItem(p, "shares")->valueint;
    double notional = cJSON_GetObjectItem(p, "notional_usd")->valuedouble;
    if (!id || !canon) return;

    render_prompt(p);

    /* TTL: parse expires_at against NTP-synced clock; skeleton uses 60s. */
    int seconds_left = 60;

    key_t k;
    while ((k = wait_key(seconds_left * 1000)) != KEY_NONE) {
        if (k == KEY_RIGHT || k == KEY_LEFT) break;
        /* KEY_CENTER = detail view (TODO); UP/DOWN ignored in prompt mode. */
    }

    char pb_line[PASSBOOK_LINE_LEN];
    snprintf(pb_line, sizeof(pb_line), "%s %s %dsh $%.0f",
             action ? action : "?", symbol ? symbol : "?", shares, notional);

    if (k == KEY_RIGHT) {
        sign_and_respond(id, canon, 1);
        passbook_append('A', broker ? broker : "?", pb_line);
    } else if (k == KEY_LEFT) {
        sign_and_respond(id, canon, 0);
        passbook_append('D', broker ? broker : "?", pb_line);
    } else {
        ESP_LOGI(TAG, "no tap; letting %s expire", id);
        passbook_append('X', broker ? broker : "?", pb_line);
    }
}

/* --------------------------------------------------------------------- */
/* poll + idle loop                                                      */
/* --------------------------------------------------------------------- */

static void poll_loop(void) {
    #define SEEN_SIZE 16
    char seen[SEEN_SIZE][64] = {0};
    int seen_ix = 0;
    uint16_t pb_cursor = 0;
    char url[256]; snprintf(url, sizeof(url), "%s/intents/pending", SHIM_URL_BASE);
    char *rxbuf = malloc(HTTP_RX_BUF);

    render_passbook(pb_cursor);

    while (1) {
        memset(rxbuf, 0, HTTP_RX_BUF);
        rx_t rx = { .buf = rxbuf, .cap = HTTP_RX_BUF };
        int status = http_get(url, &rx);
        int handled_any = 0;

        if (status == 200) {
            cJSON *root = cJSON_Parse(rxbuf);
            if (root) {
                cJSON *prompts = cJSON_GetObjectItem(root, "prompts");
                cJSON *p;
                cJSON_ArrayForEach(p, prompts) {
                    const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(p, "intent_id"));
                    if (!id) continue;
                    int already = 0;
                    for (int i = 0; i < SEEN_SIZE; i++)
                        if (strcmp(seen[i], id) == 0) { already = 1; break; }
                    if (already) continue;
                    strncpy(seen[seen_ix], id, 63);
                    seen_ix = (seen_ix + 1) % SEEN_SIZE;
                    handle_prompt(p);
                    handled_any = 1;
                }
                cJSON_Delete(root);
            }
        }

        if (!handled_any) {
            /* passbook nav while idle */
            key_t k = wait_key(POLL_INTERVAL_MS);
            if (k == KEY_UP && pb_cursor > 0) pb_cursor--;
            else if (k == KEY_DOWN && pb_cursor + 1 < g_pb.count) pb_cursor++;
            if (k != KEY_NONE) render_passbook(pb_cursor);
        } else {
            render_passbook(pb_cursor);
        }
    }
}

/* --------------------------------------------------------------------- */
/* app entry                                                             */
/* --------------------------------------------------------------------- */

void app_main(void) {
    ESP_LOGI(TAG, "tradecard booting");

    esp_err_t nvs = nvs_flash_init();
    if (nvs == ESP_ERR_NVS_NO_FREE_PAGES || nvs == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    if (sodium_init() < 0) { ESP_LOGE(TAG, "libsodium init failed"); return; }

    gpio_setup();
    lcd_init();
    passbook_load();
    ESP_ERROR_CHECK(keys_load_or_create());
    wifi_bringup();
    register_if_needed();
    poll_loop();
}
