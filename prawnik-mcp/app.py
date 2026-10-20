"""Read-only MCP over official ELI, Sejm and SAOS endpoints. All results need legal interpretation."""
import os,re,json,hmac
from datetime import datetime,timezone
from hashlib import sha256
from difflib import unified_diff
from html.parser import HTMLParser
from urllib.parse import urlencode
import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE={'eli':'https://api.sejm.gov.pl','saos':'https://www.saos.org.pl'}
CATALOG={
'CBOSA WSA/NSA':'https://orzeczenia.nsa.gov.pl','SN':'https://www.sn.pl/pl/wyszukiwarka-orzeczen',
'TK':'https://trybunal.gov.pl','EUREKA KIS/KAS':'https://eureka.mf.gov.pl',
'ZUS':'https://bip.zus.pl/inne/interpretacje','PIP':'https://www.pip.gov.pl',
'CURIA':'https://curia.europa.eu','EUR-Lex':'https://eur-lex.europa.eu',
'HUDOC':'https://hudoc.echr.coe.int','RCL':'https://legislacja.rcl.gov.pl'}
def check(s,maxlen=180):
    if not isinstance(s,str) or not 0<len(s.strip())<=maxlen: raise ValueError('Invalid or empty query')
    if re.search(r'\b\d{11}\b|[^\s@]+@[^\s@]+\.[^\s@]+',s): raise ValueError('Private identifier in query')
    return s.strip()
def publication(p,y,n):
    if p not in ('DU','MP') or type(y)!=int or not 1918<=y<=2100 or type(n)!=int or not 1<=n<=999999: raise ValueError('Invalid publication identifier')
    return f'/eli/acts/{p}/{y}/{n}'
def get(source,path,params=None,html=False):
    if source not in BASE or not path.startswith('/') or path.startswith('//') or '..' in path or '://' in path:raise ValueError('Disallowed source')
    with httpx.Client(timeout=12,follow_redirects=False,trust_env=False,headers={'User-Agent':'Prawnik-legal-research/0.3','Accept':'text/html' if html else 'application/json'}) as client:
        r=client.get(BASE[source]+path,params=params)
        r.raise_for_status()
        if len(r.content)>1000000:raise ValueError('Source too large')
        data=r.text if html else r.json()
        digest=sha256(r.content).hexdigest()
        return {'status':'ODCZYTANE','source':source,'source_url':str(r.url),'retrieved_utc':datetime.now(timezone.utc).isoformat(),'sha256':digest,'data':data}
class StripHTML(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip-=1 if self.skip else 0
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)
    def text(self):return re.sub(r'\s+',' ',' '.join(self.parts)).strip()
def article(p,y,n,a):
    publication(p,y,n)
    if not re.fullmatch(r'[0-9]{1,4}[a-zA-Z]?',a):raise ValueError('Invalid article identifier')
    z=get('eli',publication(p,y,n)+f'/text.html/artykul={a}',html=True)
    h=StripHTML();h.feed(z['data'])
    z['data']={'article':a,'text':h.text()[:25000]}
    z['limitation']='This is one published text, not a legal as-of-date consolidation.'
    return z

mcp=FastMCP('Prawnik zrodla',host='0.0.0.0',port=int(os.getenv('PORT','8000')),stateless_http=True,json_response=True)
@mcp.tool()
def eli_search_acts(title:str,publisher:str='DU',year:int|None=None,limit:int=10)->dict:
    """Search legally published acts by title and optional year; no time-validity conclusion."""
    if publisher not in ('DU','MP') or not 1<=limit<=50:raise ValueError('Invalid arguments')
    params={'title':check(title),'publisher':publisher,'limit':limit}
    if year is not None:
        if not 1918<=year<=2100:raise ValueError('Invalid year')
        params['year']=year
    return get('eli','/eli/acts/search',params)
@mcp.tool()
def eli_get_act(publisher:str,year:int,position:int)->dict:
    """Fetch a Polish legal act metadata, references, versions and official ELI address."""
    r=get('eli',publication(publisher,year,position))
    r['official_url']=f'https://eli.gov.pl/eli/{publisher}/{year}/{position}/ogl'
    r['limitation']='Status of entire act does not prove applicability of specific article on a historical date.'
    return r
@mcp.tool()
def eli_get_article(publisher:str,year:int,position:int,article:str)->dict:
    """Read article of a specified act when its HTML is available."""
    return article(publisher,year,position,article)
@mcp.tool()
def eli_get_change_candidates(publisher:str,year:int,position:int)->dict:
    """Find potential historical amendment and repeal references without declaring legal validity."""
    r=eli_get_act(publisher,year,position)
    d=r['data']
    r['references_to_check']=d.get('references',{}) if isinstance(d,dict) else {}
    r['legal_applicability']='NIEUSTALONA; requires dates, amendment text and transitional provisions.'
    return r
@mcp.tool()
def sejm_get_print(term:int,number:str)->dict:
    """Retrieve Sejm parliamentary print metadata, not binding law."""
    if type(term)!=int or not 1<=term<=30 or not re.fullmatch(r'[0-9]+(?:-[A-Za-z0-9]+)?',number):raise ValueError('Invalid print')
    z=get('eli',f'/sejm/term{term}/prints/{number}')
    z['limitation']='A parliamentary draft is not promulgated law.'
    return z
@mcp.tool()
def saos_find_judgments(phrase:str='',case_number:str='',legal_base:str='',from_date:str='',to_date:str='',page:int=0,page_size:int=10)->dict:
    """Search the non-exhaustive SAOS case law corpus by legal basis, keywords, case ID and dates."""
    if not any((phrase,case_number,legal_base)) or not 0<=page<=1000 or not 10<=page_size<=100:raise ValueError('Invalid filters')
    q={'pageNumber':page,'pageSize':page_size,'sortingField':'JUDGMENT_DATE','sortingDirection':'DESC'}
    for k,v in [('all',phrase),('caseNumber',case_number),('legalBase',legal_base)]:
        if v:q[k]=check(v)
    for k,v in [('judgmentDateFrom',from_date),('judgmentDateTo',to_date)]:
        if v:
            from datetime import date
            date.fromisoformat(v);q[k]=v
    z=get('saos','/api/search/judgments',q)
    z['limitation']='SAOS is incomplete and non-official. Similarity of facts and law must be reviewed.'
    return z
@mcp.tool()
def saos_get_judgment(judgment_id:int)->dict:
    """Fetch a SAOS judgment by database identifier; verify official court source separately."""
    if type(judgment_id)!=int or judgment_id<=0:raise ValueError('Invalid judgment ID')
    z=get('saos',f'/api/judgments/{judgment_id}')
    z['limitation']='This is secondary publication, not official validation of status.'
    return z
@mcp.tool()
def eli_compare_article_texts(first:dict,second:dict,max_diff_lines:int=100)->dict:
    """Diff two retrieved act fragments, not a definitive determination of law applicability."""
    if not 5<=max_diff_lines<=400:raise ValueError('Invalid diff size')
    x=article(first['publisher'],first['year'],first['position'],first['article'])
    y=article(second['publisher'],second['year'],second['position'],second['article'])
    a=x['data']['text'];b=y['data']['text']
    changes=list(unified_diff(a.split(),b.split(),lineterm=''))
    return {'identical':a==b,'first_url':x['source_url'],'second_url':y['source_url'],
            'first_sha256':x['sha256'],'second_sha256':y['sha256'],
            'diff':changes[:max_diff_lines],'diff_truncated':len(changes)>max_diff_lines,
            'legal_applicability':'NIEUSTALONA; investigate amendments and transitional rules.'}
@mcp.tool()
def official_source_directory()->dict:
    """List verified provider websites for sources without configured automated APIs."""
    return {'automated':['ELI API','Sejm API','SAOS API'],'manual_sources':CATALOG,
            'warning':'No automatic live access to manual sources; consult their original documents.'}

class BearerGate:
    def __init__(self,app):
        self.app=app
        self.token=os.getenv('PRAWNIK_MCP_TOKEN','')
        if len(self.token)<32:raise RuntimeError('PRAWNIK_MCP_TOKEN must contain >=32 random chars')
    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':return await self.app(scope,receive,send)
        if scope['type'] not in ('http','websocket'):return await self.app(scope,receive,send)
        headers={k.lower():v for k,v in scope.get('headers',[])}
        candidate=headers.get(b'authorization',b'').decode('ascii','ignore')
        if not hmac.compare_digest(candidate,'Bearer '+self.token):
            if scope['type']=='websocket':await send({'type':'websocket.close','code':1008})
            else:
                await send({'type':'http.response.start','status':401,'headers':[(b'www-authenticate',b'Bearer realm="Prawnik"'),(b'content-type',b'text/plain')]})
                await send({'type':'http.response.body','body':b'Unauthorized'})
            return
        return await self.app(scope,receive,send)

host=os.getenv('RENDER_EXTERNAL_HOSTNAME','').strip() or os.getenv('PRAWNIK_ALLOWED_HOST','localhost').strip()
if not re.fullmatch(r'[a-zA-Z0-9.-]+',host):raise RuntimeError('Invalid public hostname')
security=TransportSecuritySettings(allowed_hosts=[host,host+':*','localhost','localhost:*','127.0.0.1','127.0.0.1:*'],
    allowed_origins=['https://chatgpt.com','https://chat.openai.com'])
app=BearerGate(mcp.streamable_http_app(transport_security=security))
if __name__=='__main__':uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8000')))
