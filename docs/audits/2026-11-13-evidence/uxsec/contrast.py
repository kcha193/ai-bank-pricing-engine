def lum(h):
    h=h.lstrip('#'); r,g,b=[int(h[i:i+2],16)/255 for i in (0,2,4)]
    f=lambda c: c/12.92 if c<=0.03928 else ((c+0.055)/1.055)**2.4
    return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b)
def cr(a,b):
    la,lb=sorted([lum(a),lum(b)],reverse=True); return (la+0.05)/(lb+0.05)
toks={"NAVY":"#0B1F3A","BLUE":"#1E40AF","CHART_BLUE":"#4C78A8","TEAL":"#0E8C8C","SLATE":"#7C8DA6","MUTED":"#A9B6C8","AMBER":"#C77C02","RED":"#C2410C","GREEN":"#2F855A","CHART_INK":"#607D9F","DARK_TEXT":"#E6ECF5"}
bgs={"light_bg #F8FAFC":"#F8FAFC","dark_bg #0B1220":"#0B1220","white":"#FFFFFF"}
for t,v in toks.items():
    print(f"{t:10s} {v}  "+"  ".join(f"{n}: {cr(v,b):.2f}" for n,b in bgs.items()))
