import sys,types
sys.path.insert(0,r'C:\quant_trading\MyRepo')
# Mock the exact shapes the SDK can return, without any API key involved.
class Blk:
    def __init__(self,t,**kw):
        self.type=t
        for k,v in kw.items(): setattr(self,k,v)
class Resp:
    def __init__(self,c): self.content=c
def extract(resp):
    text = "".join(b.text for b in resp.content if getattr(b,"type",None)=="text").strip()
    if not text:
        raise RuntimeError("no text block (got: %s)" % [getattr(b,'type','?') for b in resp.content])
    return text
P=F=0
def ck(n,c,d=''):
    global P,F
    if c: P+=1; print(f'  PASS  {n}')
    else: F+=1; print(f'  FAIL  {n} {d}')
# the shape that broke it daily since Aug 13
r=Resp([Blk('thinking',thinking='hmm...'),Blk('text',text='{"a":1}')])
try: ck('ThinkingBlock first -> still extracts text', extract(r)=='{"a":1}')
except Exception as e: ck('ThinkingBlock first',False,str(e))
# the old code path, for contrast
try:
    r.content[0].text; ck('old code would have crashed',False,'unexpectedly worked')
except AttributeError: ck('old code would have crashed (AttributeError)',True)
ck('plain text block', extract(Resp([Blk('text',text='ok')]))=='ok')
ck('multiple text blocks concatenate',
   extract(Resp([Blk('text',text='a'),Blk('text',text='b')]))=='ab')
try:
    extract(Resp([Blk('thinking',thinking='only thinking')])); ck('no text -> raises',False)
except RuntimeError: ck('no text block -> clear RuntimeError',True)
print(f"\nRESULT: {P} passed, {F} failed")
sys.exit(1 if F else 0)
