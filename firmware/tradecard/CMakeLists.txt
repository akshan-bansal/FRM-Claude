# TradeCard — ESP-IDF top-level project file.
#
# Build (from this directory, ESP-IDF 5.x env sourced):
#   idf.py set-target esp32s3
#   idf.py menuconfig     # set Wi-Fi SSID/PSK + shim URL under "TradeCard"
#   idf.py build flash monitor
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(tradecard)
