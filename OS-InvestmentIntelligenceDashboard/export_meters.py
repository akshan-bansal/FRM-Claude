"""Read-only TradeCard snapshot exporter. No broker calls, orders, credentials or device probes."""
import argparse,csv,json,math,sqlite3
from pathlib import Path
from datetime import datetime,timezone

def dt(s):
 d=datetime.fromisoformat(s.replace('Z','+00:00'))
 if d.tzinfo is None: raise ValueError('Timezone required')
 return d.astimezone(timezone.utc)
def finite(v):
 v=float(v)
 if not math.isfinite(v): raise ValueError('Nonfinite reading')
 return v

def export(state,session,currency,now=None,stale_seconds=900,db=None):
 now=now or datetime.now(timezone.utc)
 out={'schema_version':1,'mode':'PAPER SNAPSHOT','captured_at':now.isoformat(),'readings':{},'notes':[]}
 def put(i,value,unit,source,as_of,scope,reason='',kind='number'):
  if kind=='number':value=finite(value)
  age=(now-dt(as_of)).total_seconds()
  if age < 0:raise ValueError('Future source timestamp')
  out['readings'][f'{i:03}']=dict(value=value,unit=unit,source=source,as_of=as_of,scope=scope,reason=reason,status='STALE' if age>stale_seconds else 'OBSERVED',stale_after_seconds=stale_seconds,kind=kind)
 eq=state/'paper_equity.csv'
 if eq.exists():
  with eq.open(newline='') as f:rows=[r for r in csv.DictReader(f) if r.get('session_id')==session]
  rows.sort(key=lambda r:dt(r['ts']))
  if rows:
   r=rows[-1]; ts=r['ts'];scope=f'Paper session {session}; currency {currency} supplied by operator'
   nav,cash,positions,peak=[finite(r[k]) for k in ('equity','cash','positions_value','peak_equity')]
   if abs(nav-cash-positions)>max(.02,abs(nav)*1e-6):raise ValueError('NAV does not reconcile with cash + signed positions value')
   put(1,nav,currency,eq.name+':equity',ts,scope)
   put(2,cash,currency,eq.name+':cash',ts,scope)
   put(3,finite(r['realized_pnl'])+finite(r['unrealized_pnl']),currency,eq.name+':realized_pnl + unrealized_pnl',ts,scope,'Repository P&L components, not total return; may exclude fees.')
   if nav>0:put(6,100*positions/nav,'%',eq.name+':positions_value/equity',ts,scope,'Signed net exposure only; gross cannot be inferred.')
   dd=finite(r['drawdown_pct'])
   if dd<0 or dd>1:raise ValueError('Expected positive fractional drawdown')
   put(9,100*dd,'%',eq.name+':drawdown_pct',ts,scope,'Current drawdown, not maximum history.')
   if peak>0:put(10,100*nav/peak,'%',eq.name+':equity/peak_equity',ts,scope)
   out['notes'].append('Opening equity and external flows are not in this export; return is intentionally unavailable.')
  else:out['notes'].append('No equity records for selected session.')
 else:out['notes'].append('paper_equity.csv absent; no portfolio readings fabricated.')
 fills=state/'paper_fills.jsonl'
 if fills.exists():
  rows=[]
  for ln,line in enumerate(fills.read_text().splitlines(),1):
   if not line.strip():continue
   try:r=json.loads(line)
   except ValueError:raise ValueError(f'Malformed paper fill at line {ln}')
   if r.get('session_id')==session:rows.append(r)
  if rows:
   ts=max(rows,key=lambda r:dt(r['fill_time']))['fill_time']
   put(88,len(rows),'fills',fills.name,ts,f'Paper session {session}','Count of journal records; historical count through last fill, not proof of current liveness.')
 # Read only summary columns, never key material, account IDs or canonical payloads.
 if db:
  conn=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
  try:
   conn.execute('BEGIN')
   rs=conn.execute('SELECT issued_at,expires_at,resolved_at,verdict FROM intents').fetchall()
   stamp=now.isoformat(); scope='Entire selected approval database; not filtered to paper session'
   put(81,len(rs),'intents',db.name+':intents',stamp,scope)
   for i,verdict in [(85,'ACCEPT'),(86,'DECLINE')]:put(i,sum(r[3]==verdict for r in rs),'decisions',db.name+':intents.verdict',stamp,scope)
   pending=sum(r[3] is None and dt(r[1])>now for r in rs)
   put(84,pending,'prompts',db.name+':unexpired unresolved',stamp,scope)
   put(112,pending,'prompts',db.name+':unexpired unresolved',stamp,scope)
   put(100,sum(r[3] is not None for r in rs),'records',db.name+':resolved verdicts',stamp,scope,'Full retained database count; not PWA page count.')
   times=[(dt(r[2])-dt(r[0])).total_seconds() for r in rs if r[3] in ('ACCEPT','DECLINE') and r[2]]
   if any(t<0 for t in times):raise ValueError('Negative response interval in database')
   out['approval_response_mean_seconds']=sum(times)/len(times) if times else None
   out['notes'].append('Response mean uses resolved_at − issued_at and excludes expiry; no fixed 4.2-second substitute.')
  finally:conn.close()
 return out
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state-dir',type=Path,required=True);p.add_argument('--session',required=True);p.add_argument('--currency',required=True);p.add_argument('--approval-db',type=Path);p.add_argument('--stale-seconds',type=float,default=900);p.add_argument('--output',type=Path,default=Path('snapshot.json'));a=p.parse_args()
 if a.stale_seconds<=0: p.error('stale-seconds must be positive')
 data=export(a.state_dir,a.session,a.currency,stale_seconds=a.stale_seconds,db=a.approval_db)
 a.output.write_text(json.dumps(data,indent=2,allow_nan=False));print(f'{len(data["readings"])} sourced readings written to {a.output}')
