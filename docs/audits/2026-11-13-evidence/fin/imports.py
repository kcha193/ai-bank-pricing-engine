import ast, pathlib, json, collections
root=__import__('pathlib').Path(__file__).resolve().parents[4].joinpath("src/balance_pricing_engine")
mods={}
for p in root.rglob("*.py"):
    rel=p.relative_to(root.parent).with_suffix("")
    name=".".join(rel.parts).removesuffix(".__init__")
    mods[name]=p
def pkg(n): 
    parts=n.split(".")
    return parts[1] if len(parts)>1 else "<root>"
edges=collections.defaultdict(set); lazy=collections.defaultdict(set)
for n,p in mods.items():
    tree=ast.parse(p.read_text())
    top_level=set(id(x) for x in tree.body)
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            targets=[]
            if isinstance(node,ast.ImportFrom) and node.module:
                base=node.module
                if node.level:
                    b=n.split("."); b=b[:len(b)-node.level+ (1 if p.name=="__init__.py" else 0)]
                    base=".".join(b+[node.module]) if node.module else ".".join(b)
                targets=[base]
            elif isinstance(node,ast.Import):
                targets=[a.name for a in node.names]
            for t in targets:
                if t.startswith("balance_pricing_engine"):
                    (edges if id(node) in top_level or True else lazy)[n].add(t)
# package-level edges
pk=collections.defaultdict(set)
for a,bs in edges.items():
    for b in bs:
        if pkg(a)!=pkg(b): pk[pkg(a)].add(pkg(b))
# check rule
viol=[]
for a,bs in edges.items():
    if pkg(a) in ("optimization","pricing","scenarios"):
        for b in bs:
            if b.startswith("balance_pricing_engine.pricing_intelligence") and b!="balance_pricing_engine.pricing_intelligence.strategy":
                viol.append((a,b))
pi_dash=[(a,b) for a,bs in edges.items() for b in bs if pkg(a)=="pricing_intelligence" and pkg(b)=="dashboard"]
llm_into_core=[(a,b) for a,bs in edges.items() for b in bs if pkg(a) in ("optimization","pricing","scenarios","application","governance") and pkg(b)=="llm"]
# SCC on module graph (resolve to known modules)
def resolve(t):
    while t and t not in mods: t=t.rsplit(".",1)[0] if "." in t else ""
    return t
g={a:{resolve(b) for b in bs if resolve(b)} for a,bs in edges.items()}
idx={};low={};st=[];on=set();sccs=[];c=[0]
import sys; sys.setrecursionlimit(10000)
def sc(v):
    idx[v]=low[v]=c[0];c[0]+=1;st.append(v);on.add(v)
    for w in g.get(v,()):
        if w not in idx: sc(w); low[v]=min(low[v],low[w])
        elif w in on: low[v]=min(low[v],idx[w])
    if low[v]==idx[v]:
        comp=[]
        while True:
            w=st.pop();on.discard(w);comp.append(w)
            if w==v:break
        if len(comp)>1: sccs.append(sorted(comp))
for v in list(g): 
    if v not in idx: sc(v)
print(json.dumps({"package_edges":{k:sorted(v) for k,v in sorted(pk.items())},"rule_violations":viol,"pricing_intelligence_imports_dashboard":pi_dash,"llm_imported_by_core":llm_into_core,"module_cycles":sccs},indent=1))
