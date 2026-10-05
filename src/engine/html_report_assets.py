"""The stylesheet and the script of every HTML report (engine.html_report), kept apart from the
Python that writes the pages.

Everything is inline and our own: no library, no font, no image, nothing loaded from anywhere.
The script is plain ES5-style JavaScript. Its pure parts (the view-state link format, the
filter language, the value encodings) are separate strings so tests can run them on their own.

Design tokens are the app's: primary #1E40AF, secondary #3B82F6, amber #D97706 (highlights
only), slate background #F8FAFC, white cards with #CBD5E1 borders, text #0F172A, muted #475569,
danger #DC2626, success #16A34A; a dark palette for prefers-color-scheme: dark (or the report's
own theme switch). Every text / background pair used keeps a contrast of at least 4.5:1
(tests check TOKENS).
"""

import json

from .timeline import ZONE_NAMES

# name -> (light, dark). Colours pages use as text on --card / --bg keep 4.5:1 or more.
TOKENS = (
    ("primary", "#1E40AF", "#93C5FD"),
    ("secondary", "#3B82F6", "#60A5FA"),
    ("amber", "#D97706", "#F59E0B"),
    ("bg", "#F8FAFC", "#0B1220"),
    ("card", "#FFFFFF", "#111827"),
    ("border", "#CBD5E1", "#334155"),
    ("line", "#E2E8F0", "#1F2937"),
    ("text", "#0F172A", "#E2E8F0"),
    ("muted", "#475569", "#94A3B8"),
    ("danger", "#DC2626", "#F87171"),
    ("success", "#16A34A", "#4ADE80"),
    ("link", "#1E40AF", "#93C5FD"),
    ("head", "#EEF2F7", "#1E293B"),
    ("zebra", "#F8FAFC", "#0F172A"),
    ("hover", "#EFF6FF", "#172554"),
    ("active", "#DBEAFE", "#1E3A8A"),
    ("focus", "#3B82F6", "#60A5FA"),
    ("bar", "#1E40AF", "#0F172A"),
    ("bar-fg", "#FFFFFF", "#E2E8F0"),
    ("bar-border", "#3B82F6", "#334155"),
    ("mark", "#FDE68A", "#F59E0B"),
    ("mark-fg", "#0F172A", "#0F172A"),
    ("b-ok-bg", "#DCFCE7", "#14532D"),
    ("b-ok-fg", "#166534", "#BBF7D0"),
    ("b-warn-bg", "#FEF3C7", "#78350F"),
    ("b-warn-fg", "#92400E", "#FDE68A"),
    ("b-bad-bg", "#FEE2E2", "#7F1D1D"),
    ("b-bad-fg", "#991B1B", "#FECACA"),
    ("b-info-bg", "#DBEAFE", "#1E3A8A"),
    ("b-info-fg", "#1E3A8A", "#BFDBFE"),
    ("b-mut-bg", "#E2E8F0", "#334155"),
    ("b-mut-fg", "#334155", "#E2E8F0"),
    ("chart", "#3B82F6", "#60A5FA"),
)

# (text token, background token) pairs the pages use for text: each >= 4.5:1 in both palettes
CONTRAST_PAIRS = (
    ("text", "card"), ("text", "bg"), ("text", "head"), ("text", "zebra"), ("text", "hover"),
    ("text", "active"), ("muted", "card"), ("muted", "bg"), ("muted", "head"),
    ("muted", "zebra"), ("link", "card"), ("link", "bg"), ("danger", "card"),
    ("bar-fg", "bar"), ("mark-fg", "mark"), ("b-ok-fg", "b-ok-bg"),
    ("b-warn-fg", "b-warn-bg"), ("b-bad-fg", "b-bad-bg"), ("b-info-fg", "b-info-bg"),
    ("b-mut-fg", "b-mut-bg"), ("primary", "card"),
)


def _vars(which):
    return "".join("--%s:%s;" % (t[0], t[1 + which]) for t in TOKENS)


LIGHT_VARS = _vars(0)
DARK_VARS = _vars(1)

CSS = (":root{" + LIGHT_VARS +
       "--font:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,"
       "sans-serif;--mono:ui-monospace,'Cascadia Mono',Consolas,'SF Mono',Menlo,monospace;"
       "--shadow:0 1px 2px rgba(15,23,42,.06),0 4px 14px rgba(15,23,42,.05);"
       "--radius:12px;--topbar:52px;color-scheme:light}\n"
       "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" + DARK_VARS +
       "--shadow:0 1px 2px rgba(0,0,0,.4);color-scheme:dark}}\n"
       ":root[data-theme=dark]{" + DARK_VARS + "--shadow:0 1px 2px rgba(0,0,0,.4);"
       "color-scheme:dark}\n" + r"""
*{box-sizing:border-box}
html{scroll-padding-top:calc(var(--topbar) + 12px);-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 var(--font)}
a{color:var(--link);text-underline-offset:2px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.vh{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip{position:absolute;left:-9999px;top:8px;z-index:100;background:var(--card);color:var(--text);padding:8px 12px;border-radius:8px}
.skip:focus{left:16px}
html:not(.js) .js-only{display:none!important}
.muted{color:var(--muted)}.small{font-size:12.5px}.mono{font-family:var(--mono);font-size:12.5px;overflow-wrap:anywhere}
.num{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:var(--danger)}.nul{color:var(--muted);font-style:italic}
mark{background:var(--mark);color:var(--mark-fg);border-radius:2px;padding:0 1px}
mark.rsm.cur{outline:2px solid var(--amber)}
/* top bar */
.topbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:12px;min-height:var(--topbar);padding:8px 16px;background:var(--bar);color:var(--bar-fg);box-shadow:0 1px 0 var(--bar-border)}
.topbar .brand{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.topbar .brand small{font-weight:400;opacity:.9;margin-left:8px}
.topbar .tools{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.btn{font:inherit;font-size:13px;line-height:1.2;border:1px solid var(--border);background:var(--card);color:var(--text);border-radius:8px;padding:6px 10px;cursor:pointer;min-height:32px;white-space:nowrap}
.btn:hover{border-color:var(--secondary)}.btn[disabled]{opacity:.45;cursor:default}
.btn.sm{min-height:26px;padding:2px 8px;font-size:12px}
.btn[aria-pressed=true]{background:var(--active)}
.topbar .btn{background:transparent;color:var(--bar-fg);border-color:var(--bar-border)}
.topbar .btn:hover{border-color:var(--bar-fg)}
/* layout */
.layout{display:grid;grid-template-columns:272px minmax(0,1fr);gap:24px;max-width:1680px;margin:0 auto;padding:16px 20px 64px}
nav.toc{position:sticky;top:calc(var(--topbar) + 16px);align-self:start;max-height:calc(100vh - var(--topbar) - 32px);overflow:auto;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 12px}
nav.toc h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:10px 4px 6px}
nav.toc ol{list-style:none;margin:0;padding:0}
nav.toc ol ol{padding-left:12px;border-left:1px solid var(--line);margin-left:6px}
nav.toc li{margin:1px 0}
nav.toc a{display:flex;gap:6px;align-items:baseline;justify-content:space-between;padding:3px 6px;border-radius:6px;color:var(--text);text-decoration:none;font-size:13.5px}
nav.toc a:hover{background:var(--hover)}
nav.toc a[aria-current=true]{background:var(--active);font-weight:600}
nav.toc .cnt{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
nav.toc .hits{color:var(--mark-fg);background:var(--mark);border-radius:999px;padding:0 6px;font-size:11.5px}
nav.toc li.rs-hide{display:none}
/* collapsible sidebar */
.layout.toc-hidden{grid-template-columns:minmax(0,1fr)}
.layout.toc-hidden nav.toc{display:none}
.toc-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:10px 4px 6px}
.toc-head h2{margin:0!important}
#toc-toggle{flex:0 0 auto}
.toc-show{position:fixed;left:12px;top:calc(var(--topbar) + 12px);z-index:50;display:none}
.layout.toc-hidden .toc-show{display:block}
.tocsearch input{width:100%;font:inherit;font-size:13px;padding:7px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)}
main{min-width:0}
main:focus{outline:none}
/* cover and sections */
.cover,.rsec{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px 26px;margin:0 0 18px}
.cover{border-top:4px solid var(--primary)}
.eyebrow{margin:0;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;font-size:12px;font-weight:650}
h1{font-size:28px;line-height:1.2;margin:6px 0 6px;overflow-wrap:anywhere}
h2{font-size:20px;line-height:1.3;margin:0 0 12px;overflow-wrap:anywhere}
h3{font-size:16px;line-height:1.35;margin:18px 0 8px;overflow-wrap:anywhere}
h4{font-size:14px;margin:14px 0 6px}
.rsec.lvl3{padding:16px 20px;margin-left:0;box-shadow:none}
.sub{color:var(--muted);margin:0 0 10px}
.statusline{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 18px;margin:14px 0}
.facts dt{color:var(--muted);font-weight:600}.facts dd{margin:0;overflow-wrap:anywhere;white-space:pre-line}
.evgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr));gap:10px;margin:8px 0}
.evcard{border:1px solid var(--border);border-radius:10px;padding:10px 12px;background:var(--bg)}
.evcard .path{font-family:var(--mono);font-size:12.5px;overflow-wrap:anywhere;margin:4px 0}
.note{border-left:4px solid var(--secondary);background:var(--bg);padding:8px 12px;border-radius:6px;margin:10px 0}
.note.warn{border-left-color:var(--amber)}.note.bad{border-left-color:var(--danger)}
p{margin:8px 0}
/* cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(190px,100%),1fr));gap:12px;margin:8px 0 14px}
.card{border:1px solid var(--border);border-radius:10px;padding:12px 14px;background:var(--bg);min-width:0}
.card .l{color:var(--muted);font-size:12px;font-weight:650;text-transform:uppercase;letter-spacing:.05em}
.card .v{font-size:22px;font-weight:650;line-height:1.25;margin:2px 0;overflow-wrap:anywhere}
.card .s{color:var(--muted);font-size:12.5px;overflow-wrap:anywhere}
.card.warn{border-left:4px solid var(--amber)}.card.bad{border-left:4px solid var(--danger)}.card.ok{border-left:4px solid var(--success)}
.card.wide{grid-column:1/-1}
/* badges and chips */
.badge{display:inline-block;padding:0 8px;border-radius:999px;font-size:12px;font-weight:600;line-height:20px;white-space:nowrap;background:var(--b-mut-bg);color:var(--b-mut-fg);vertical-align:baseline}
.badge.ok{background:var(--b-ok-bg);color:var(--b-ok-fg)}.badge.warn{background:var(--b-warn-bg);color:var(--b-warn-fg)}
.badge.bad{background:var(--b-bad-bg);color:var(--b-bad-fg)}.badge.info{background:var(--b-info-bg);color:var(--b-info-fg)}
.chip{display:inline-block;padding:0 8px;border-radius:999px;font-size:12px;font-weight:600;line-height:20px;margin:0 4px 2px 0;white-space:nowrap;box-shadow:inset 0 0 0 1px rgba(15,23,42,.18)}
.dbswatch{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:8px;vertical-align:baseline;box-shadow:inset 0 0 0 1px rgba(15,23,42,.25)}
ul.legend{list-style:none;padding:0;margin:4px 0;display:flex;flex-wrap:wrap;gap:6px 18px}ul.legend a{text-decoration:none}
.mix{display:flex;height:10px;border-radius:999px;overflow:hidden;margin:6px 0;background:var(--line)}
.mix span{display:block;height:100%}
.mix .ok{background:var(--success)}.mix .warn{background:var(--amber)}.mix .bad{background:var(--danger)}.mix .info{background:var(--secondary)}.mix .muted{background:var(--muted)}
.tops{list-style:none;margin:6px 0 0;padding:0}
.tops li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;font-size:12.5px;position:relative;padding:1px 4px}
.tops li .bar{position:absolute;left:0;top:2px;bottom:2px;background:var(--active);border-radius:3px;z-index:0}
.tops li span{position:relative;z-index:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* chart */
.chart{width:100%;height:auto;max-height:220px;display:block}
.chart .bar{fill:var(--chart)}.chart .grid{stroke:var(--line);stroke-width:1}
.chart .axis{fill:var(--muted);font-size:11px;font-family:var(--font)}
/* simple tables */
.stw{overflow-x:auto;margin:8px 0 12px;border:1px solid var(--border);border-radius:10px}
table.st{border-collapse:collapse;width:100%;font-size:13px}
.st th{background:var(--head);text-align:left;font-weight:600;padding:7px 10px;border-bottom:2px solid var(--border);vertical-align:bottom}
.st td{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere;background:var(--card)}
.st th:first-child,.st td:first-child{position:sticky;left:0;z-index:1;box-shadow:inset -1px 0 0 var(--line)}
.st tbody tr:nth-child(even) td{background:var(--zebra)}
.st tbody tr:last-child td{border-bottom:0}
.st td.v,.st td.mono{font-family:var(--mono);font-size:12.5px}
.st th .ss{all:unset;cursor:pointer;display:inline-flex;gap:4px;align-items:center}
.st th .ss:focus-visible{outline:2px solid var(--focus)}
.st tr.warn td:first-child{box-shadow:inset 4px 0 0 var(--amber)}.st tr.bad td:first-child{box-shadow:inset 4px 0 0 var(--danger)}
.st tr.ok td:first-child{box-shadow:inset 4px 0 0 var(--success)}
img.thumb{display:block;max-width:160px;max-height:120px;margin-top:4px;border:1px solid var(--border);border-radius:4px;background:#fff}
/* code, details, diagram */
.codeblock{border:1px solid var(--border);border-radius:10px;margin:8px 0 12px;overflow:hidden}
.codehead{display:flex;align-items:center;gap:8px;justify-content:space-between;padding:6px 10px;background:var(--head);font-size:12.5px;font-weight:600}
pre{margin:0;font-family:var(--mono);font-size:12.5px;white-space:pre-wrap;overflow-wrap:anywhere}
pre.code{padding:10px 12px;background:var(--bg);max-height:520px;overflow:auto}
details{margin:6px 0;border:1px solid var(--border);border-radius:10px;padding:0 12px;background:var(--card)}
details>summary{cursor:pointer;font-weight:600;padding:8px 0;list-style-position:inside}
details[open]>summary{border-bottom:1px solid var(--line);margin-bottom:8px}
.diagram{overflow:auto;border:1px solid var(--border);border-radius:10px;max-height:760px;background:#FFFFFF;margin:8px 0}
.diagram svg{display:block}
/* data grids */
.tblock{margin:16px 0 32px}
.tblock>h3,.tblock>h4{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.gtool{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:14px 0 12px}
.gtool .gsearch{flex:1 1 240px;min-width:200px;display:flex}
.gtool input[type=search]{flex:1;font:inherit;font-size:14px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--text)}
.chk{display:inline-flex;gap:6px;align-items:center;font-size:13px;white-space:nowrap}
.gstat{color:var(--muted);font-size:12.5px;margin-left:auto;font-variant-numeric:tabular-nums}
.gload{padding:18px;border:1px dashed var(--border);border-radius:10px;color:var(--muted);text-align:center}
.gload .pbar{height:6px;border-radius:999px;background:var(--line);overflow:hidden;margin:10px auto 0;max-width:320px}
.gload .pbar span{display:block;height:100%;width:0;background:var(--secondary)}
.gwrap{position:relative;overflow:auto;max-height:78vh;border:1px solid var(--border);border-radius:10px;background:var(--card)}
.gwrap:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
table.gt{border-collapse:separate;border-spacing:0;table-layout:fixed;font-size:14px;min-width:100%}
.gt thead{position:sticky;top:0;z-index:4}
.gt th{background:var(--head);text-align:left;font-weight:600;padding:0;position:relative;border-bottom:1px solid var(--border);vertical-align:bottom}
.gt .thc{display:flex;align-items:center;gap:4px;padding:9px 6px 9px 12px;min-width:0}
.gt .sortb{flex:1;min-width:0;text-align:left;background:none;border:0;font:inherit;font-weight:600;color:var(--text);cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 0}
.gt .sortb .dk{font-weight:400;color:var(--muted);font-size:11.5px}
.gt .si{color:var(--primary);font-size:11px;white-space:nowrap}
.gt .fmenu{background:none;border:1px solid transparent;border-radius:6px;color:var(--muted);cursor:pointer;padding:0 5px;line-height:20px;font-size:12px}
.gt .fmenu:hover,.gt .fmenu.on{border-color:var(--border);color:var(--primary)}
.gt .fr td{padding:6px;background:var(--head);border-bottom:2px solid var(--border)}
.gt .fi{width:100%;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--card);color:var(--text)}
.gt .fi[aria-invalid=true]{border-color:var(--danger);box-shadow:0 0 0 1px var(--danger)}
.gt tbody td{height:38px;padding:0 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid var(--line);background:var(--card)}
[data-density=compact] .gt tbody td{height:28px;padding:0 8px;font-size:13px}
.gt tbody tr.z td{background:var(--zebra)}
.gt tbody tr:hover td{background:var(--hover)}
.gt tbody tr.act td{background:var(--active)}
.gt tbody tr.act td.rn{box-shadow:inset 3px 0 0 var(--primary)}
.gt td.n{text-align:right;font-variant-numeric:tabular-nums}
.gt .rn{position:sticky;left:0;z-index:2;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.gt th.rn{z-index:5;padding:0 8px}
.gt .pin{position:sticky;left:var(--rnw,64px);z-index:2;box-shadow:1px 0 0 var(--border)}
.gt th.pin{z-index:5}
.gt .fr td.rn,.gt .fr td.pin{z-index:5}
.gt tr.sp td{padding:0;border:0;background:transparent!important}
.gt .dt{font-variant-numeric:tabular-nums}.gt .raw{color:var(--muted);font-size:11.5px}
.gwrap.wrapped .gt tbody td{height:68px;white-space:normal;line-height:1.35;padding-top:4px;padding-bottom:4px;vertical-align:top}
[data-density=compact] .gwrap.wrapped .gt tbody td{height:52px}
.gt td .cl{display:-webkit-box;-webkit-line-clamp:3;line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.gt .rz:hover{background:var(--primary);opacity:.35}
.pop .tzl{list-style:none;margin:8px 0 0;padding:0;max-height:52vh;overflow:auto}
.pop .tzl li{margin:0 0 3px}.pop .tzb{width:100%;text-align:left;white-space:normal}
.pop .tzb.on{border-color:var(--primary);color:var(--primary);font-weight:600}
.pop #tz-q{width:100%}
.overlay .panel.bpanel{max-width:1100px}
.bihead{display:flex;justify-content:space-between;align-items:center;gap:12px}.bihead h2{margin:0}
.bitabs{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.bitabs .btn.on{border-color:var(--primary);color:var(--primary);font-weight:600}
.bifind{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}
.bifind input{flex:1;min-width:220px;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}
.binav{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 6px}
.binav input{font:inherit;font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}
.bipre{max-height:58vh;overflow:auto}
.bihex{font-family:var(--mono);font-size:12.5px;line-height:1.5;white-space:pre;overflow:auto;max-height:58vh;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
#bi-body mark.cur{outline:2px solid var(--primary)}
.bioff{color:var(--primary)}
.gt .blob{color:var(--primary);font-weight:600}
.gt tr.gempty td{height:auto;white-space:normal;text-align:center;padding:28px;color:var(--muted)}
.rz{position:absolute;right:0;top:0;bottom:0;width:7px;cursor:col-resize;z-index:6}
.rz:hover{background:var(--secondary)}
.gt td.missing{color:var(--muted)}
.static-wrap{margin:8px 0}
.js .static-wrap{display:none}
/* popover, drawer, overlays */
.pop{position:fixed;z-index:60;min-width:260px;max-width:min(440px,94vw);max-height:72vh;overflow:auto;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:12px;box-shadow:0 12px 32px rgba(15,23,42,.22);padding:12px}
.pop .pophead{font-size:14px;margin-bottom:8px}
.pop .popsec{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}
.pop select,.pop input[type=text],.pop input[type=search]{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);max-width:100%}
.pop .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.pop ul{list-style:none;margin:6px 0;padding:0;max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.pop li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:3px 8px;font-size:13px}
.pop li:nth-child(even){background:var(--zebra)}
.pop li label{display:flex;gap:6px;align-items:center;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pop .cnt{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.drawer{position:fixed;z-index:50;top:var(--topbar);right:0;bottom:0;width:min(580px,94vw);background:var(--card);color:var(--text);border-left:1px solid var(--border);box-shadow:-12px 0 32px rgba(15,23,42,.18);display:flex;flex-direction:column}
.drawer[hidden]{display:none}
.dhead{padding:14px 18px 10px;border-bottom:1px solid var(--line)}
.dhead h2{font-size:17px;margin:0}
.dhead h2:focus{outline:none}
.dtools{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.dbody{padding:10px 18px 24px;overflow:auto;flex:1}
.dbody h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:16px 0 6px}
.dpath{font-weight:600;overflow-wrap:anywhere}
dl.kv{margin:0;display:grid;grid-template-columns:minmax(110px,32%) minmax(0,1fr);gap:0}
dl.kv dt{padding:7px 8px 7px 0;border-top:1px solid var(--line);font-weight:600;font-size:13px;overflow-wrap:anywhere}
dl.kv dd{margin:0;padding:7px 0;border-top:1px solid var(--line);min-width:0}
dl.kv dt .cp{margin-top:4px;display:block}
pre.val{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;max-height:320px;overflow:auto}
.dts span{display:block}
.overlay{position:fixed;inset:0;z-index:70;background:rgba(15,23,42,.45);display:flex;align-items:flex-start;justify-content:center;padding:6vh 16px}
.overlay[hidden]{display:none}
.overlay .panel{background:var(--card);color:var(--text);border-radius:14px;max-width:760px;width:100%;max-height:86vh;overflow:auto;padding:20px 24px;box-shadow:0 20px 48px rgba(0,0,0,.35)}
.keys{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px}
kbd{font-family:var(--mono);font-size:12px;border:1px solid var(--border);border-bottom-width:2px;border-radius:5px;padding:0 6px;background:var(--bg)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:80;background:var(--text);color:var(--card);padding:8px 14px;border-radius:10px;font-size:13px;box-shadow:var(--shadow)}
.toast[hidden]{display:none}
.totop{position:fixed;right:20px;bottom:20px;z-index:45}
.totop[hidden]{display:none}
.print-only{display:none}
footer.foot{color:var(--muted);font-size:12.5px;text-align:center;padding:10px 0 0}
html{scroll-behavior:auto}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}.js nav.toc{transition:transform .2s ease,visibility .2s}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
.toc-btn{display:none}
@media screen and (min-width:1440px){.layout{grid-template-columns:300px minmax(0,1fr);gap:28px;padding:20px 28px 72px}}
@media screen and (max-width:1023px){
.layout{grid-template-columns:minmax(0,1fr);padding:12px 12px 56px}
nav.toc{position:static;max-height:none;margin-bottom:12px}
.js .toc-btn{display:inline-flex;align-items:center}
.js nav.toc{position:fixed;top:var(--topbar);left:0;bottom:0;z-index:55;width:min(340px,88vw);max-height:none;margin:0;border-radius:0;border-width:0 1px 0 0;transform:translateX(-105%);visibility:hidden;box-shadow:12px 0 32px rgba(15,23,42,.2)}
.js.toc-open nav.toc{transform:none;visibility:visible}
.topbar .brand small{display:none}
.cover,.rsec{padding:16px}
}
@media screen and (max-width:767px){
html{scroll-padding-top:12px}
body{font-size:15px}
.topbar{position:static;flex-wrap:wrap}
.topbar .tools{margin-left:0;width:100%;justify-content:flex-start}
.js nav.toc{top:0;z-index:95;width:88vw}
.layout{padding:8px 8px 56px;gap:12px}
.cover,.rsec{padding:14px 12px;border-radius:10px}
h1{font-size:23px}h2{font-size:18px}
.facts{grid-template-columns:minmax(0,1fr);gap:0 0}.facts dd{margin:0 0 8px}
dl.kv{grid-template-columns:minmax(0,1fr)}dl.kv dt{padding-bottom:0}dl.kv dd{border-top:0}
.drawer{top:0;width:100vw;border-left:0;z-index:90}
.gwrap{max-height:65vh}
.gstat{margin-left:0;width:100%}
.small,.muted.small,.badge,.chip,.card .l,.card .s,.mono,pre,.gt,.st,.tops li,nav.toc a,nav.toc .cnt,.gstat,.pop li,.pop .cnt,.btn,.btn.sm,.gt .fi,.gt .raw,.gt .sortb .dk,.codehead,kbd,.evcard .path,.gtool input[type=search],.tocsearch input,.pop select,.pop input[type=text],.pop input[type=search],.chk,footer.foot{font-size:14px}
}
@media screen and (max-width:767px),screen and (pointer:coarse){
.btn,.btn.sm,.gt .fmenu,.tocsearch input,.gtool input[type=search],.pop select,.pop input[type=text],.pop input[type=search],nav.toc a,.chk,.gt .fi,.st th .ss{min-height:44px}
.gt .fmenu,.btn.sm{min-width:44px}
.chk input,.pop li input{width:22px;height:22px}
.pop li{min-height:44px}
html .gt tbody td,[data-density=compact] .gt tbody td{height:44px}
.rz{width:14px}
}
@media print{
:root,:root[data-theme]{""" + LIGHT_VARS + r"""color-scheme:light}
body{background:#FFFFFF;font-size:10.5pt}
.topbar,nav.toc,.gtool,.grid,.drawer,.overlay,.pop,.toast,.totop,.btn,.skip,.js-only,noscript{display:none!important}
.print-only{display:block}
.layout{display:block;padding:0;max-width:none}
.cover,.rsec{box-shadow:none;border:0;padding:0;margin:0 0 12pt;border-radius:0}
.cover{break-after:page;border-top:0}
.rsec.lvl2{break-before:page}
.js .static-wrap,.static-wrap{display:block!important}
.stw{overflow:visible;border:0}
.st th{position:static}
tr,img,.card,.evcard{break-inside:avoid}
thead{display:table-header-group}
h2,h3,h4{break-after:avoid}
a{color:inherit;text-decoration:none}
.diagram{max-height:none;overflow:visible;border:0}
pre.code,pre.val{max-height:none;overflow:visible}
details{border:0;padding:0}
}
""")

# -- the view-state link format (#v=1&tbl=...&f=...): mirrored by html_report.state_encode ---------
STATE_JS = r"""
function stEnc(st){
  var p=['v=1'],E=encodeURIComponent;st=st||{};
  if(st.sec)p.push('sec='+E(st.sec));
  if(st.tbl)p.push('tbl='+E(st.tbl));
  if(st.q)p.push('q='+E(st.q));
  if(st.m===0)p.push('m=0');
  if(st.sort&&st.sort.length)p.push('sort='+st.sort.map(function(s){return s[0]+(s[1]<0?'d':'a');}).join(','));
  if(st.f){
    var ks=Object.keys(st.f).filter(function(k){return /^\d+$/.test(k);}).map(Number).sort(function(a,b){return a-b;}),o={},n=0;
    ks.forEach(function(k){var x=st.f[k]||{},y={};if(x.e)y.e=String(x.e);if(x.v)y.v=x.v.map(String);if(y.e||y.v){o[k]=y;n++;}});
    if(n)p.push('f='+E(JSON.stringify(o)));
  }
  if(typeof st.row==='number'&&st.row>=0)p.push('row='+st.row);
  if(st.hide&&st.hide.length)p.push('hide='+st.hide.join(','));
  if(st.ord&&st.ord.length)p.push('ord='+st.ord.join(','));
  if(st.pin)p.push('pin=1');
  return p.join('&');
}
function stDec(h){
  var st={sec:'',tbl:'',q:'',m:1,sort:[],f:{},row:-1,hide:[],ord:[],pin:0};
  h=String(h||'');if(h.charAt(0)==='#')h=h.slice(1);
  if(!h)return st;
  var dec=function(s){try{return decodeURIComponent(s);}catch(e){return '';}};
  if(h.indexOf('=')<0){st.sec=dec(h);return st;}
  var ints=function(s){return s.split(',').filter(function(x){return /^\d+$/.test(x);}).map(Number);};
  h.split('&').forEach(function(kv){
    var i=kv.indexOf('=');if(i<0)return;
    var k=kv.slice(0,i),v=kv.slice(i+1);
    if(k==='sec')st.sec=dec(v);
    else if(k==='tbl')st.tbl=dec(v);
    else if(k==='q')st.q=dec(v);
    else if(k==='m')st.m=v==='0'?0:1;
    else if(k==='sort')v.split(',').forEach(function(s){var m=/^(\d+)([ad])$/.exec(s);if(m)st.sort.push([Number(m[1]),m[2]==='d'?-1:1]);});
    else if(k==='f'){
      try{
        var o=JSON.parse(dec(v));
        if(o&&typeof o==='object'&&!Array.isArray(o))Object.keys(o).forEach(function(c){
          if(!/^\d+$/.test(c))return;
          var x=o[c]||{},y={};
          if(typeof x.e==='string'&&x.e)y.e=x.e;
          if(Array.isArray(x.v))y.v=x.v.map(String);
          if(y.e||y.v)st.f[c]=y;
        });
      }catch(e){}
    }
    else if(k==='row'){if(/^\d+$/.test(v))st.row=Number(v);}
    else if(k==='hide')st.hide=ints(v);
    else if(k==='ord')st.ord=ints(v);
    else if(k==='pin')st.pin=v==='1'?1:0;
  });
  return st;
}
"""

# -- values as embedded (see html_report.encode_cell) and their CSV / JSON text ------------------
VALUES_JS = r"""
var ESC={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return ESC[c];});}
function fmtInt(n){n=String(n);var neg=n.charAt(0)==='-';if(neg)n=n.slice(1);var o='';while(n.length>3){o=','+n.slice(-3)+o;n=n.slice(0,-3);}return (neg?'-':'')+n+o;}
function isObj(v){return v!==null&&typeof v==='object';}
function blobText(b){return 'BLOB '+fmtInt(b.n)+' bytes'+(b.s?' \u00b7 '+b.s:'');}
function cellText(v){
  if(v===null||v===undefined)return 'NULL';
  var t=typeof v;
  if(t==='string')return v;
  if(t==='number')return String(v);
  if(v.i!==undefined)return v.i;
  if(v.f!==undefined)return v.f;
  if(v.r!==undefined)return v.r;
  if(v.x!==undefined)return v.t;
  if(v.b!==undefined)return blobText(v.b);
  if(v.m!==undefined)return '';
  if(v.j!==undefined)return JSON.stringify(plainJ(v.j));
  return String(v);
}
function plainJ(x){
  if(Array.isArray(x))return x.map(cellPlain);
  var o={};Object.keys(x).forEach(function(k){o[k]=cellPlain(x[k]);});return o;
}
function cellPlain(c){if(!isObj(c))return c;if(c.j!==undefined)return plainJ(c.j);return cellText(c);}
function cellKey(v){return v===null||v===undefined?'\u0000NULL':cellText(v);}
function rankOf(v){
  if(v===null||v===undefined)return 0;
  var t=typeof v;if(t==='number')return 1;if(t==='string')return 2;
  if(v.i!==undefined||v.f!==undefined||v.r!==undefined)return 1;
  if(v.b!==undefined)return 3;
  return 2;
}
function numOf(v){
  if(typeof v==='number')return v;
  if(v.i!==undefined)return Number(v.i);
  if(v.f!==undefined)return Number(v.f);
  if(v.r!==undefined)return v.r==='inf'?Infinity:(v.r==='-inf'?-Infinity:NaN);
  return NaN;
}
function partNote(b){return b.p?' [first '+b.p.kept+' of '+b.n+' bytes, SHA-256 of all '+b.p.h+']':'';}
function csvCell(v){
  if(v===null||v===undefined)return 'NULL';
  var t=typeof v;
  if(t==='string')return v;
  if(t==='number')return String(v);
  if(v.i!==undefined)return v.i;
  if(v.f!==undefined)return v.f;
  if(v.r!==undefined)return v.r;
  if(v.x!==undefined)return v.t;
  if(v.m!==undefined)return '';
  if(v.b!==undefined){
    var b=v.b;
    if(b.hex!==undefined)return "x'"+b.hex+"'"+partNote(b);
    if(b.b64!==undefined)return 'base64:'+b.b64+partNote(b);
    return '[BLOB '+b.n+' bytes, SHA-256 '+(b.p?b.p.h:(b.h||'?'))+']';
  }
  if(v.j!==undefined)return jsonText(v,true);
  return String(v);
}
function RawNum(s){this.s=s;}
var INT_RE=/^-?\d+$/,REAL_RE=/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/;
function jv(v){
  if(v===null||v===undefined)return null;
  var t=typeof v;
  if(t==='string'||t==='number')return v;
  if(v.i!==undefined)return INT_RE.test(String(v.i))?new RawNum(String(v.i)):String(v.i);
  if(v.f!==undefined)return REAL_RE.test(String(v.f))?new RawNum(String(v.f)):String(v.f);
  if(v.r!==undefined)return {real:v.r};
  if(v.x!==undefined)return {invalid_text_hex:v.x};
  if(v.m!==undefined)return null;
  if(v.b!==undefined){
    var b=v.b,o={};
    if(b.hex!==undefined){o.blob_hex=b.hex;o.size=b.n;}
    else if(b.b64!==undefined){o.blob_base64=b.b64;o.size=b.n;}
    else{o.blob_summary=b.s||'';o.size=b.n;o.sha256=b.p?b.p.h:(b.h||null);}
    if(b.p)o.partial={bytes_kept:b.p.kept,sha256:b.p.h};
    return o;
  }
  if(v.j!==undefined){
    var x=v.j;
    if(Array.isArray(x))return x.map(jv);
    var r=Object.create(null);Object.keys(x).forEach(function(k){r[k]=jv(x[k]);});return r;
  }
  return String(v);
}
function jsonStr(x,sp){
  if(x===null||x===undefined)return 'null';
  var t=typeof x,c=sp?', ':',',k=sp?': ':':';
  if(t==='string'||t==='number'||t==='boolean')return JSON.stringify(x);
  if(x instanceof RawNum)return x.s;
  if(Array.isArray(x))return '['+x.map(function(y){return jsonStr(y,sp);}).join(c)+']';
  return '{'+Object.keys(x).map(function(y){return JSON.stringify(y)+k+jsonStr(x[y],sp);}).join(c)+'}';
}
function jsonText(v,sp){return jsonStr(jv(v),sp);}
function csvQuote(s){s=String(s);return /[",\r\n]|^\s|\s$/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
/* text: NUL as \x00 and a ' before = + - @ TAB CR, as every CSV of the tool (engine.csvcells) */
function csvField(s){s=String(s).replace(/\x00/g,'\\x00');if(/^[=+\-@\t\r]/.test(s))s="'"+s;return csvQuote(s);}
function isNum(v){return typeof v==='number'||(!!v&&typeof v==='object'&&(v.i!==undefined||v.f!==undefined||v.r!==undefined));}
function csvValue(v){return isNum(v)?csvQuote(csvCell(v)):csvField(csvCell(v));}
function pad(n,w){var s=String(n);while(s.length<(w||2))s='0'+s;return s;}
/* a zone: false or 0 = UTC, true or 'local' = this browser's local time (daylight saving
   included), a number = minutes east of UTC */
function zoneOff(ms,tz){
  if(tz===true||tz==='local'){var d=new Date(ms);return isNaN(d.getTime())?0:-d.getTimezoneOffset();}
  return typeof tz==='number'?tz:0;
}
function offLabel(m){return m?'UTC'+(m<0?'-':'+')+pad(Math.floor(Math.abs(m)/60))+':'+pad(Math.abs(m)%60):'UTC';}
function fmtDate(ms,tz){
  if(ms===null||ms===undefined||typeof ms!=='number')return '';
  var d=new Date(ms+zoneOff(ms,tz)*60000);if(isNaN(d.getTime()))return '';
  var y=d.getUTCFullYear(),f=d.getUTCMilliseconds();
  var t=(y<0?'-':'')+pad(Math.abs(y),4)+'-'+pad(d.getUTCMonth()+1)+'-'+pad(d.getUTCDate())+' '+pad(d.getUTCHours())+':'+pad(d.getUTCMinutes())+':'+pad(d.getUTCSeconds());
  return f?t+'.'+pad(f,3):t;
}
"""

# -- the filter language (a practical subset of the app's engine.filters) ------------------------
FILTER_JS = r"""
var NUM_RE=/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
var DATE_RE=/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/;
function unq(s){
  var m=/^"((?:[^"]|"")*)"$/.exec(s);if(m)return m[1].replace(/""/g,'"');
  m=/^'((?:[^']|'')*)'$/.exec(s);if(m)return m[1].replace(/''/g,"'");
  return null;
}
function dateMs(s,tz){
  var m=DATE_RE.exec(s);if(!m)return null;
  var a=[Number(m[1]),Number(m[2])-1,Number(m[3]),Number(m[4]||0),Number(m[5]||0),Number(m[6]||0)];
  if(tz===true||tz==='local')return new Date(a[0],a[1],a[2],a[3],a[4],a[5]).getTime();
  return Date.UTC(a[0],a[1],a[2],a[3],a[4],a[5])-(typeof tz==='number'?tz:0)*60000;
}
function operand(s,local){
  s=String(s).trim();
  var q=unq(s);if(q!==null)return {k:'text',v:q};
  if(/^NULL$/i.test(s))return {k:'null'};
  if(NUM_RE.test(s))return {k:'num',v:parseFloat(s)};
  var m=/^[xX]'((?:[0-9a-fA-F]{2})*)'$/.exec(s);if(m)return {k:'blob',v:m[1].toLowerCase()};
  var d=dateMs(s,local);if(d!==null)return {k:'date',v:d,s:s};
  return {k:'text',v:s};
}
function splitItems(s){
  var out=[],cur='',q='',i,c;
  for(i=0;i<s.length;i++){
    c=s.charAt(i);
    if(q){cur+=c;if(c===q){if(s.charAt(i+1)===q){cur+=c;i++;}else q='';}}
    else if(c==='"'||c==="'"){q=c;cur+=c;}
    else if(c===','){out.push(cur);cur='';}
    else cur+=c;
  }
  out.push(cur);
  return out.map(function(x){return x.trim();}).filter(function(x){return x.length>0;});
}
function likeRe(p){
  var r='',i,c;
  for(i=0;i<p.length;i++){c=p.charAt(i);r+=c==='%'?'[\\s\\S]*':c==='_'?'[\\s\\S]':c.replace(/[.*+?^${}()|[\]\\\/]/g,'\\$&');}
  return new RegExp('^'+r+'$','i');
}
var AFFIX=[['!^=','nstarts'],['!$=','nends'],['!*=','ncontains'],['^=','starts'],['$=','ends'],['*=','contains']];
var OPS=['>=','<=','<>','!=','>','<','='];
function parseFilter(expr,local){
  var s=String(expr===null||expr===undefined?'':expr).trim(),m,i,u,q;
  if(!s)return null;
  if(/^NULL$/i.test(s))return {t:'null'};
  if(/^NOT\s+NULL$/i.test(s))return {t:'nnull'};
  if(/^EMPTY$/i.test(s))return {t:'empty'};
  if(/^NOT\s+EMPTY$/i.test(s))return {t:'nempty'};
  m=/^(NOT\s+)?IN\s*\(([\s\S]*)\)$/i.exec(s);
  if(m)return {t:m[1]?'nin':'in',items:splitItems(m[2]).map(function(x){return operand(x,local);})};
  m=/^\/([\s\S]*)\/([iI]?)$/.exec(s);
  if(m)return {t:'re',re:new RegExp(m[1],m[2]?'i':'')};
  for(i=0;i<AFFIX.length;i++){
    if(s.indexOf(AFFIX[i][0])===0){u=s.slice(AFFIX[i][0].length);q=unq(u.trim());return {t:AFFIX[i][1],s:(q!==null?q:u).toLowerCase()};}
  }
  for(i=0;i<OPS.length;i++){
    if(s.indexOf(OPS[i])===0)return {t:'cmp',op:OPS[i]==='!='?'<>':OPS[i],o:operand(s.slice(OPS[i].length),local)};
  }
  u=unq(s);if(u!==null)return {t:'eq',s:u};
  i=s.indexOf('~');
  if(i>0&&i<s.length-1){
    var a=operand(s.slice(0,i),local),b=operand(s.slice(i+1),local);
    if(a.k===b.k&&(a.k==='num'||a.k==='date'||a.k==='text'))return {t:'range',a:a,b:b};
  }
  if(s.charAt(0)==='!'&&s.length>1){u=s.slice(1);return u.indexOf('%')>=0?{t:'nlike',re:likeRe(u)}:{t:'ncontains',s:u.toLowerCase()};}
  if(s.indexOf('%')>=0)return {t:'like',re:likeRe(s)};
  return {t:'contains',s:s.toLowerCase()};
}
function isNull(v){return v===null||v===undefined;}
function cmpVal(v,ms,o){
  if(isNull(v)||o.k==='null')return null;
  if(o.k==='date'&&typeof ms==='number')return ms<o.v?-1:(ms>o.v?1:0);
  var rv=rankOf(v),ro=o.k==='num'?1:(o.k==='blob'?3:2);
  if(rv!==ro)return rv<ro?-1:1;
  if(rv===1){var a=numOf(v),b=o.v;return a<b?-1:(a>b?1:0);}
  if(rv===3){var h=v.b.hex;if(h===undefined)return null;return h<o.v?-1:(h>o.v?1:0);}
  var s=cellText(v),t=o.k==='date'?o.s:String(o.v);
  return s<t?-1:(s>t?1:0);
}
function testFilter(f,v,ms){
  var l,c,i,it,x,y;
  switch(f.t){
  case 'null':return isNull(v);
  case 'nnull':return !isNull(v);
  case 'empty':return isNull(v)||v==='';
  case 'nempty':return !(isNull(v)||v==='');
  case 'contains':return !isNull(v)&&cellText(v).toLowerCase().indexOf(f.s)>=0;
  case 'ncontains':return isNull(v)||cellText(v).toLowerCase().indexOf(f.s)<0;
  case 'starts':return !isNull(v)&&cellText(v).toLowerCase().indexOf(f.s)===0;
  case 'nstarts':return isNull(v)||cellText(v).toLowerCase().indexOf(f.s)!==0;
  case 'ends':if(isNull(v))return false;l=cellText(v).toLowerCase();return l.length>=f.s.length&&l.slice(l.length-f.s.length)===f.s;
  case 'nends':if(isNull(v))return true;l=cellText(v).toLowerCase();return !(l.length>=f.s.length&&l.slice(l.length-f.s.length)===f.s);
  case 're':case 'like':return !isNull(v)&&f.re.test(cellText(v));
  case 'nlike':return isNull(v)||!f.re.test(cellText(v));
  case 'eq':return typeof v==='string'&&v===f.s;
  case 'cmp':
    c=cmpVal(v,ms,f.o);if(c===null)return false;
    switch(f.op){case '=':return c===0;case '<>':return c!==0;case '>':return c>0;case '>=':return c>=0;case '<':return c<0;case '<=':return c<=0;}
    return false;
  case 'range':x=cmpVal(v,ms,f.a);y=cmpVal(v,ms,f.b);return x!==null&&y!==null&&x>=0&&y<=0;
  case 'in':
    for(i=0;i<f.items.length;i++){it=f.items[i];if(it.k==='null'){if(isNull(v))return true;}else if(cmpVal(v,ms,it)===0)return true;}
    return false;
  case 'nin':
    var hasNull=false;
    for(i=0;i<f.items.length;i++){it=f.items[i];if(it.k==='null'){hasNull=true;}else if(cmpVal(v,ms,it)===0)return false;}
    return isNull(v)?!hasNull:true;
  }
  return true;
}
function condExpr(op,a,b){
  var lit=function(s){s=String(s);return NUM_RE.test(s.trim())||DATE_RE.test(s.trim())?s.trim():'"'+s.replace(/"/g,'""')+'"';};
  var aff=function(s){s=String(s);return /^\s|\s$/.test(s)||/^["']/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;};
  switch(op){
  case 'contains':return '*='+aff(a);
  case 'ncontains':return '!*='+aff(a);
  case 'starts':return '^='+aff(a);
  case 'ends':return '$='+aff(a);
  case 'eq':return '='+lit(a);
  case 'ne':return '<>'+lit(a);
  case 'gt':return '>'+lit(a);
  case 'ge':return '>='+lit(a);
  case 'lt':return '<'+lit(a);
  case 'le':return '<='+lit(a);
  case 'between':return String(a).trim()+'~'+String(b).trim();
  case 'empty':return 'EMPTY';
  case 'nempty':return 'NOT EMPTY';
  case 'null':return 'NULL';
  case 'nnull':return 'NOT NULL';
  case 're':return '/'+a+'/i';
  }
  return '';
}
"""

# -- the page: grids, drawer, filters, search, navigation, preferences, print ------------------
MAIN_JS = r"""
(function(){
'use strict';
var D=document,W=window,R=D.documentElement;
R.className=(R.className?R.className+' ':'')+'js';
/*PURE*/
var now=W.performance&&W.performance.now?function(){return W.performance.now();}:function(){return Date.now();};
var prefs={theme:'',density:'',tz:'utc',wrap:0},PK='sga-report-prefs';
try{
  var ps=W.localStorage.getItem(PK);
  if(ps){var po=JSON.parse(ps);if(po&&typeof po==='object'){
    if(po.theme==='light'||po.theme==='dark')prefs.theme=po.theme;
    if(po.density==='compact')prefs.density='compact';
    if(po.tz==='local')prefs.tz='local';
    else if(typeof po.tz==='number'&&po.tz>=-720&&po.tz<=840&&po.tz%15===0)prefs.tz=po.tz;
    if(po.wrap===1)prefs.wrap=1;}}
}catch(e){}
function savePrefs(){try{W.localStorage.setItem(PK,JSON.stringify(prefs));}catch(e){}}
function applyPrefs(){
  if(prefs.theme)R.setAttribute('data-theme',prefs.theme);else R.removeAttribute('data-theme');
  R.setAttribute('data-density',prefs.density||'comfortable');
}
applyPrefs();
function isLocal(){return prefs.tz==='local';}
function curTz(){return prefs.tz==='local'?'local':(typeof prefs.tz==='number'?prefs.tz:0);}
function tzName(){return offLabel(-new Date().getTimezoneOffset());}
function zoneText(m){var n=ZONES[String(m)];return offLabel(m)+(n&&m?' \u00b7 '+n:'');}
/* the zone dates are shown in, in words: 'UTC', 'Local time (UTC+05:30)', 'UTC+09:00 · Japan, Korea' */
function tzText(){var z=curTz();return z==='local'?'Local time ('+tzName()+')':zoneText(z);}
function rowH(){return (prefs.density==='compact'?28:38)+(prefs.wrap?(prefs.density==='compact'?24:30):0);}
var grids=[],gridById={},lastGrid=null,hashGrid=null,curSec='',live=false,hashT=0;
var pop,popAnchor=null,popGrid=null,popCol=-1,popKind='';
var dr,drBody,drTitle,drPos,drG=null,drP=-1,help,helpReturn=null,toast,toastT=0,toTop;
var COLOR_RE=/^#[0-9a-fA-F]{6}$/;
function $(id){return D.getElementById(id);}
function say(msg){if(!toast)return;toast.textContent=msg;toast.hidden=false;clearTimeout(toastT);toastT=setTimeout(function(){toast.hidden=true;},2600);}
function copyText(text,what){
  var fallback=function(){
    var ta=D.createElement('textarea'),ok=false;ta.value=text;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.left='-9999px';
    D.body.appendChild(ta);ta.select();try{ok=D.execCommand('copy');}catch(e){ok=false;}D.body.removeChild(ta);
    say(ok?what+' copied':'Copying is blocked here: select the text and press Ctrl+C');
  };
  if(W.navigator.clipboard&&W.navigator.clipboard.writeText){W.navigator.clipboard.writeText(text).then(function(){say(what+' copied');},fallback);}
  else fallback();
}
function download(name,text,type){
  var blob=new Blob([text],{type:type}),url=URL.createObjectURL(blob),a=D.createElement('a');
  a.href=url;a.download=name;D.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(url);if(a.parentNode)a.parentNode.removeChild(a);},4000);
  say('Download started: '+name);
}
function safeName(s){return String(s).replace(/[^A-Za-z0-9._-]+/g,'_').replace(/^_+|_+$/g,'').slice(0,80)||'rows';}
function debounce(fn,ms){var t=0;return function(){var a=arguments,self=this;clearTimeout(t);t=setTimeout(function(){fn.apply(self,a);},ms);};}
function hl(s,q){
  s=String(s);if(!q)return esc(s);
  var l=s.toLowerCase(),i=l.indexOf(q);if(i<0)return esc(s);
  var o='',j=0,n=0;
  while(i>=0&&n<20){o+=esc(s.slice(j,i))+'<mark>'+esc(s.slice(i,i+q.length))+'</mark>';j=i+q.length;i=l.indexOf(q,j);n++;}
  return o+esc(s.slice(j));
}
function hexToB64(hex){var s='',i;for(i=0;i+1<hex.length;i+=2)s+=String.fromCharCode(parseInt(hex.substr(i,2),16));return W.btoa(s);}
function hexDump(hex,max){
  var out=[],n=Math.min(hex.length/2,max),i,j,line,asc,b;
  for(i=0;i<n;i+=16){
    line=pad(i.toString(16),8)+'  ';asc='';
    for(j=i;j<i+16;j++){if(j<n){b=parseInt(hex.substr(2*j,2),16);line+=hex.substr(2*j,2)+' ';asc+=b>=32&&b<127?String.fromCharCode(b):'.';}else line+='   ';}
    out.push(line+' '+asc);
  }
  return out.join('\n');
}
/* ------------------------------------------------------------------ grids */
function Grid(m){
  var g=this;
  g.m=m;g.id=m.id;g.el=$('g-'+m.id);g.rows=[];g.view=null;g.nf=m.fields.length;g.loaded=0;g.ready=false;
  g.first=m.first||1;g.gen=0;g.q='';g.qh='';g.mo=1;g.sort=[];g.filt={};g.hide={};g.pin=0;g.act=-1;g.actCol=-1;g.openRow=-1;
  g.pendingRow=-1;g.pending=null;g.keys={};g.pin=W.innerWidth<768?1:0;g.rt=null;g.fbase=null;g.hits=0;g.measured=false;g.raf=0;g.id0=null;
  g.ord=[];for(var c=0;c<g.nf;c++)g.ord.push(c);
  g.w=(m.widths||[]).slice();for(c=0;c<g.nf;c++)if(!g.w[c])g.w[c]=140;
  g.dix={};g.dinfo={};(m.dates||[]).forEach(function(d){g.dix[d[0]]=d[3];g.dinfo[d[0]]=d;});
  g.num={};(m.num||[]).forEach(function(c){g.num[c]=1;});
  g.badges={};Object.keys(m.badges||{}).forEach(function(k){g.badges[Number(k)]=m.badges[k];});
  g.tagc=m.tags?m.tags.col:-1;g.tagColors=m.tags?m.tags.colors||{}:{};
  g.rnw=Math.max(48,String(g.first+(m.n||0)).length*8+26);
  g.rh=rowH();
  g.build();
}
Grid.prototype.build=function(){
  var g=this,m=g.m,nm=esc(m.name),h=[];
  h.push('<div class="gtool" role="toolbar" aria-label="Tools for '+nm+'">');
  h.push('<label class="gsearch"><span class="vh">Search '+nm+'</span><input type="search" class="gq" placeholder="Search this table (press /)" autocomplete="off" spellcheck="false"></label>');
  h.push('<label class="chk"><input type="checkbox" class="gmo" checked> Matches only</label>');
  h.push('<label class="chk" title="Show long values on up to three lines"><input type="checkbox" class="gwr"'+(prefs.wrap?' checked':'')+'> Wrap text</label>');
  h.push('<button type="button" class="btn" data-g="cols" aria-haspopup="dialog">Columns</button>');
  h.push('<button type="button" class="btn" data-g="dl" aria-haspopup="dialog">Download</button>');
  h.push('<button type="button" class="btn" data-g="clear">Clear filters</button>');
  h.push('<span class="gstat" role="status" aria-live="polite"></span></div>');
  h.push('<div class="gload" role="status"><span class="gloadt">Loading '+fmtInt(m.n||0)+' rows\u2026</span><div class="pbar"><span></span></div></div>');
  h.push('<div class="gwrap" tabindex="0" role="region" aria-label="'+nm+': rows (j / k move, Enter opens a row)" hidden>');
  h.push('<table class="gt" aria-rowcount="'+((m.n||0)+2)+'"><colgroup></colgroup><thead></thead><tbody></tbody></table></div>');
  g.el.innerHTML=h.join('');
  g.qEl=g.el.querySelector('.gq');g.moEl=g.el.querySelector('.gmo');g.stat=g.el.querySelector('.gstat');
  g.load=g.el.querySelector('.gload');g.loadT=g.el.querySelector('.gloadt');g.loadBar=g.el.querySelector('.pbar span');
  g.wrap=g.el.querySelector('.gwrap');g.table=g.el.querySelector('table.gt');g.cg=g.table.querySelector('colgroup');
  g.thead=g.table.querySelector('thead');g.tbody=g.table.querySelector('tbody');
  g.wrap.addEventListener('scroll',function(){if(!g.raf)g.raf=W.requestAnimationFrame(function(){g.raf=0;g.paint();});},{passive:true});
  var fq=debounce(function(){g.q=g.qEl.value;g.qh=g.q.trim().toLowerCase();lastGrid=g;hashGrid=g;g.refilter();},250);
  g.qEl.addEventListener('input',fq);
  g.moEl.addEventListener('change',function(){g.mo=g.moEl.checked?1:0;g.refilter();});
  g.wrEl=g.el.querySelector('.gwr');g.wrap.classList.toggle('wrapped',!!prefs.wrap);
  g.wrEl.addEventListener('change',function(){prefs.wrap=g.wrEl.checked?1:0;savePrefs();
    grids.forEach(function(x){if(x.wrEl)x.wrEl.checked=!!prefs.wrap;x.wrap.classList.toggle('wrapped',!!prefs.wrap);});repaintAll();});
  g.fdeb=debounce(function(c,val){g.setExpr(c,val);},300);
};
Grid.prototype.progress=function(){
  var p=this.m.chunks?Math.round(100*this.loaded/this.m.chunks):100;
  this.loadT.textContent='Loading '+fmtInt(this.m.n||0)+' rows\u2026 '+p+'%';this.loadBar.style.width=p+'%';
};
Grid.prototype.finish=function(){
  var g=this;if(g.ready)return;g.ready=true;
  g.load.hidden=true;g.wrap.hidden=false;
  if(g.err){g.load.hidden=false;g.loadT.textContent=g.err;}
  if(g.pending){g.applyState(g.pending);g.pending=null;}
  g.head();g.refilter();
};
Grid.prototype.ident=function(){
  if(!this.id0||this.id0.length!==this.rows.length){var a=new Array(this.rows.length);for(var i=0;i<a.length;i++)a[i]=i;this.id0=a;}
  return this.id0;
};
Grid.prototype.vis=function(){var g=this,o=[];g.ord.forEach(function(c){if(!g.hide[c])o.push(c);});return o;};
Grid.prototype.tableWidth=function(){var g=this,w=g.rnw;g.vis().forEach(function(c){w+=g.w[c];});return w;};
Grid.prototype.sortPos=function(c){for(var k=0;k<this.sort.length;k++)if(this.sort[k][0]===c)return k;return -1;};
Grid.prototype.head=function(){
  var g=this,cols=g.vis(),cg=['<col style="width:'+g.rnw+'px">'],h1=['<tr class="hr"><th class="rn" scope="col">#</th>'],h2=['<tr class="fr"><td class="rn"></td>'];
  cols.forEach(function(c,k){
    var name=g.m.fields[c],pin=g.pin&&k===0?' pin':'',sp=g.sortPos(c),dir=sp>=0?g.sort[sp][1]:0,f=g.filt[c],di=g.dinfo[c];
    var ind=sp>=0?(dir>0?'\u25b2':'\u25bc')+(g.sort.length>1?String(sp+1):''):'';
    cg.push('<col style="width:'+g.w[c]+'px">');
    h1.push('<th scope="col" class="c'+pin+'" data-c="'+c+'" aria-sort="'+(sp===0?(dir>0?'ascending':'descending'):'none')+'"><div class="thc">'+
      '<button type="button" class="sortb" data-c="'+c+'" title="Sort by '+esc(name)+' (Shift+click adds it to the sort)'+(di?'\n'+esc(name)+': dates read as '+esc(di[2])+', shown in '+esc(tzText()):'')+'">'+esc(name)+
      (di?' <span class="dk">date</span>':'')+'</button><span class="si" aria-hidden="true">'+ind+'</span>'+
      '<button type="button" class="fmenu'+(f?' on':'')+'" data-c="'+c+'" aria-haspopup="dialog" aria-label="Filter and options for '+esc(name)+'">\u25be</button></div>'+
      '<span class="rz" data-c="'+c+'" title="Drag to resize; double-click to fit the values" aria-hidden="true"></span></th>');
    h2.push('<td class="'+pin.trim()+'"><input class="fi" data-c="'+c+'" value="'+esc(f&&f.e||'')+'" placeholder="'+(f&&f.v?'values chosen: '+f.v.length:'filter')+
      '" aria-label="Filter '+esc(name)+'" spellcheck="false" autocomplete="off"></td>');
  });
  g.cg.innerHTML=cg.join('');
  g.thead.innerHTML=h1.join('')+'</tr>'+h2.join('')+'</tr>';
  g.table.style.width=g.tableWidth()+'px';
  g.table.style.setProperty('--rnw',g.rnw+'px');
  Object.keys(g.filt).forEach(function(c){g.markBad(Number(c),g.filt[c].bad||'');});
};
Grid.prototype.markBad=function(c,msg){
  var i=this.thead.querySelector('.fi[data-c="'+c+'"]');if(!i)return;
  if(msg){i.setAttribute('aria-invalid','true');i.title=msg;}else{i.removeAttribute('aria-invalid');i.title='';}
};
Grid.prototype.rowText=function(i){
  var g=this;if(!g.rt)g.rt=new Array(g.rows.length);
  var t=g.rt[i];
  if(t===undefined){
    var r=g.rows[i],a=[],c,v;
    for(c=0;c<g.nf;c++){v=r[c];if(!isNull(v))a.push(cellText(v));if(isObj(v)&&v.b&&v.b.dv)a.push(v.b.dv);if(g.dix[c]!==undefined&&typeof r[g.dix[c]]==='number'){a.push(fmtDate(r[g.dix[c]],false));a.push(fmtDate(r[g.dix[c]],true));if(typeof curTz()==='number')a.push(fmtDate(r[g.dix[c]],curTz()));}}
    t=g.rt[i]=a.join('\u0001').toLowerCase();
  }
  return t;
};
Grid.prototype.setExpr=function(c,val){
  var f=this.filt[c]||{};f.e=val;if(!f.e&&!f.v)delete this.filt[c];else this.filt[c]=f;
  var b=this.thead.querySelector('.fmenu[data-c="'+c+'"]');if(b)b.classList.toggle('on',!!this.filt[c]);
  this.refilter();
};
Grid.prototype.busy=function(msg){this.stat.textContent=msg+'\u2026';this.el.setAttribute('aria-busy','true');};
Grid.prototype.refilter=function(){
  var g=this,gen=++g.gen,preds=[];
  if(!g.ready)return;
  Object.keys(g.filt).forEach(function(k){
    var c=Number(k),f=g.filt[k],p={c:c,di:g.dix[c],f:null,set:null};f.bad='';
    if(f.e){try{p.f=parseFilter(f.e,curTz());}catch(e){f.bad=String(e.message||e);p.f=null;}}
    g.markBad(c,f.bad);
    if(f.v){p.set=Object.create(null);f.v.forEach(function(x){p.set[x]=1;});}
    if(p.f||p.set)preds.push(p);
  });
  var q=g.qh,n=g.rows.length,rows=g.rows,out=[],hits=0,i=0;
  if(!preds.length&&!q){g.hits=0;g.fbase=null;g.resort();return;}
  g.busy('Filtering');
  (function step(){
    if(gen!==g.gen)return;
    var end=Math.min(n,i+15000),r,ok,k,p,v,hit;
    for(;i<end;i++){
      r=rows[i];ok=true;
      for(k=0;k<preds.length;k++){
        p=preds[k];v=r[p.c];
        if(p.set&&!(cellKey(v) in p.set)){ok=false;break;}
        if(p.f&&!testFilter(p.f,v,p.di===undefined?null:r[p.di])){ok=false;break;}
      }
      if(!ok)continue;
      if(q){hit=g.rowText(i).indexOf(q)>=0;if(hit)hits++;else if(g.mo)continue;}
      out.push(i);
    }
    if(i<n){g.busy('Filtering '+Math.floor(100*i/n)+'%');setTimeout(step,0);return;}
    g.hits=hits;g.fbase=out;g.resort();
  })();
};
Grid.prototype.sortKeys=function(c){
  var g=this,K=g.keys[c];if(K)return K;
  var n=g.rows.length,r=new Uint8Array(n),x=new Float64Array(n),s=new Array(n),i,v,rk;
  for(i=0;i<n;i++){
    v=g.rows[i][c];rk=rankOf(v);r[i]=rk;
    if(rk===1)x[i]=numOf(v);
    else if(rk===3)s[i]=v.b.hex!==undefined?v.b.hex:(v.b.s||'');
    else if(rk===2)s[i]=cellText(v).toLowerCase();
  }
  K=g.keys[c]={r:r,n:x,s:s};return K;
};
Grid.prototype.resort=function(){
  var g=this,gen=g.gen;
  if(!g.sort.length){g.setView(g.fbase?g.fbase:g.ident());return;}
  g.busy('Sorting');
  setTimeout(function(){
    if(gen!==g.gen)return;
    var arr=(g.fbase?g.fbase:g.ident()).slice(),spec=g.sort,keys=[];
    spec.forEach(function(s){keys.push(g.sortKeys(s[0]));});
    arr.sort(function(a,b){
      for(var k=0;k<spec.length;k++){
        var d=spec[k][1],K=keys[k],ra=K.r[a],rb=K.r[b];
        if(ra!==rb)return (ra-rb)*d;
        if(ra===1){var x=K.n[a],y=K.n[b];if(x<y)return -d;if(x>y)return d;}
        else if(ra>=2){var s=K.s[a],t=K.s[b];if(s<t)return -d;if(s>t)return d;}
      }
      return a-b;
    });
    if(gen===g.gen)g.setView(arr);
  },16);
};
Grid.prototype.setView=function(v){
  var g=this,keep=drG===g&&g.openRow>=0?v.indexOf(g.openRow):-1;
  g.view=v;g.act=keep;
  g.el.removeAttribute('aria-busy');
  g.wrap.scrollTop=0;g.measured=false;g.paint();
  if(keep>=0){g.scrollTo(keep);g.paint();}
  g.status();tocCount(g);
  if(g.pendingRow>=0){var p=v.indexOf(g.pendingRow);g.pendingRow=-1;if(p>=0){g.act=p;g.scrollTo(p);openDrawer(g,p,false);}}
  else if(drG===g){if(keep>=0){drP=keep;renderDrawer();}else closeDrawer();}
  scheduleHash(null);
};
Grid.prototype.filtered=function(){return Object.keys(this.filt).length>0;};
Grid.prototype.describe=function(){
  var g=this,parts=[];
  if(g.q)parts.push('search "'+g.q+'"'+(g.mo?'':' (highlight only)'));
  Object.keys(g.filt).forEach(function(c){var f=g.filt[c],nm=g.m.fields[c];if(f.e)parts.push(nm+': '+f.e);if(f.v)parts.push(nm+': '+f.v.length+' value'+(f.v.length===1?'':'s')+' chosen');});
  if(g.sort.length)parts.push('sorted by '+g.sort.map(function(s){return g.m.fields[s[0]]+(s[1]>0?' ascending':' descending');}).join(', '));
  return parts.join('; ');
};
Grid.prototype.status=function(){
  var g=this,n=g.rows.length,s=fmtInt(n)+' row'+(n===1?'':'s')+' \u00b7 '+fmtInt(g.view.length)+' shown';
  if(g.qh&&!g.mo)s+=' \u00b7 '+fmtInt(g.hits)+' match'+(g.hits===1?'':'es');
  if(g.m.part)s+=' \u00b7 part '+g.m.part.no;
  g.stat.textContent=s;g.stat.title=g.describe();
};
Grid.prototype.paint=function(){
  var g=this;if(!g.view||g.wrap.hidden)return;
  var n=g.view.length,cols=g.vis(),rh=g.rh,sc=g.wrap,hh=g.thead.offsetHeight||0,vh=Math.max(sc.clientHeight-hh,rh*6),span=cols.length+1,h=[];
  if(!n){
    g.tbody.innerHTML='<tr class="gempty"><td colspan="'+span+'"><p>'+(g.rows.length?'No rows match the search and filters.':'This table has no rows.')+'</p>'+
      (g.rows.length?'<button type="button" class="btn" data-g="clear">Clear the search and filters</button>':'')+'</td></tr>';
    return;
  }
  var top=Math.min(Math.floor(sc.scrollTop/rh),n-1),first=Math.max(0,top-6),last=Math.min(n,top+Math.ceil(vh/rh)+6),p;
  if(first>0)h.push('<tr class="sp" aria-hidden="true"><td colspan="'+span+'" style="height:'+(first*rh)+'px"></td></tr>');
  for(p=first;p<last;p++)h.push(g.rowHtml(p,cols));
  if(last<n)h.push('<tr class="sp" aria-hidden="true"><td colspan="'+span+'" style="height:'+((n-last)*rh)+'px"></td></tr>');
  g.tbody.innerHTML=h.join('');
  if(!g.measured){
    g.measured=true;
    var tr=g.tbody.querySelector('tr[data-p]');
    if(tr){var rh2=tr.getBoundingClientRect().height;if(rh2>4&&Math.abs(rh2-rh)>0.01){g.rh=rh2;g.paint();}}
  }
};
Grid.prototype.rowHtml=function(p,cols){
  var g=this,i=g.view[p],r=g.rows[i],s='<tr data-p="'+p+'" aria-rowindex="'+(p+3)+'" class="'+(p&1?'z':'')+(p===g.act?' act':'')+'"><td class="rn">'+fmtInt(i+g.first)+'</td>',k,c,cls;
  for(k=0;k<cols.length;k++){c=cols[k];cls=(g.pin&&k===0?'pin':'')+(g.num[c]?' n':'');s+='<td'+(cls?' class="'+cls.trim()+'"':'')+'>'+(prefs.wrap?'<div class="cl">'+g.cellHtml(c,r)+'</div>':g.cellHtml(c,r))+'</td>';}
  return s+'</tr>';
};
function chipsHtml(g,v){
  var names=[];
  if(isObj(v)&&Array.isArray(v.j))names=v.j.map(cellText);
  else if(!isNull(v)&&cellText(v))names=cellText(v).split(/;\s*/);
  return names.map(function(nm){
    var c=g.tagColors[nm]||['#94A3B8','#0F172A'],bg=COLOR_RE.test(c[0])?c[0]:'#94A3B8',fg=COLOR_RE.test(c[1])?c[1]:'#0F172A';
    return '<span class="chip" style="background:'+bg+';color:'+fg+'">'+hl(nm,g.qh)+'</span>';
  }).join('');
}
Grid.prototype.cellHtml=function(c,r){
  var g=this,v=r[c],q=g.qh,di,t,s;
  if(c===g.tagc)return chipsHtml(g,v);
  if(isNull(v))return '<span class="nul">NULL</span>';
  di=g.dix[c];
  if(di!==undefined&&typeof r[di]==='number')return '<span class="dt">'+hl(fmtDate(r[di],curTz()),q)+'</span> <span class="raw">'+hl(cellText(v),q)+'</span>';
  if(g.badges[c]){t=cellText(v);return '<span class="badge '+(g.badges[c][t]||'muted')+'">'+hl(t,q)+'</span>';}
  if(isObj(v)){
    if(v.b!==undefined)return '<span class="blob">BLOB '+fmtInt(v.b.n)+' B</span> '+hl(v.b.s||'',q);
    if(v.x!==undefined)return '<span class="bad">invalid text</span> '+hl(v.t,q);
    if(v.m!==undefined)return '';
  }
  s=cellText(v);if(s.length>300)s=s.slice(0,300)+'\u2026';
  return hl(s,q);
};
Grid.prototype.cellPlain=function(c,r){
  var g=this,v=r[c],di=g.dix[c];
  if(isNull(v))return 'NULL';
  if(c===g.tagc)return isObj(v)&&Array.isArray(v.j)?v.j.map(cellText).join('   '):cellText(v);
  if(di!==undefined&&typeof r[di]==='number')return fmtDate(r[di],curTz())+' '+cellText(v);
  if(isObj(v)&&v.b!==undefined)return 'BLOB '+fmtInt(v.b.n)+' B '+(v.b.s||'');
  var s=cellText(v);return s.length>300?s.slice(0,301):s;
};
Grid.prototype.fit=function(c){
  var g=this,cv=fit.cv||(fit.cv=D.createElement('canvas')),x=cv.getContext('2d'),td=g.tbody.querySelector('td:not(.rn)'),th=g.thead.querySelector('th[data-c="'+c+'"] .sortb');
  var cs=W.getComputedStyle(td||g.table);x.font=cs.fontWeight+' '+cs.fontSize+' '+cs.fontFamily;
  var w=th?th.scrollWidth+58:80,v=g.view||[],step=Math.max(1,Math.floor(v.length/3000)),i,t;
  for(i=0;i<v.length;i+=step){t=x.measureText(g.cellPlain(c,g.rows[v[i]])).width+30;if(t>w)w=t;}
  g.w[c]=Math.round(Math.max(56,Math.min(prefs.wrap?560:900,w)));g.head();g.measured=false;g.paint();scheduleHash(g);
};
function fit(){}
Grid.prototype.visibleRows=function(){var hh=this.thead.offsetHeight||0;return Math.max(1,Math.floor((this.wrap.clientHeight-hh)/this.rh));};
Grid.prototype.scrollTo=function(p){
  var g=this,sc=g.wrap,hh=g.thead.offsetHeight||0,vh=sc.clientHeight-hh,top=p*g.rh;
  if(top<sc.scrollTop)sc.scrollTop=top;else if(top+g.rh>sc.scrollTop+vh)sc.scrollTop=top+g.rh-vh;
};
Grid.prototype.move=function(d){
  var g=this;if(!g.view||!g.view.length)return;
  var p=g.act<0?0:Math.max(0,Math.min(g.view.length-1,g.act+d));
  g.act=p;g.scrollTo(p);g.paint();
  if(drG===g&&!dr.hidden){drP=p;g.openRow=g.view[p];renderDrawer();scheduleHash(g);}
  g.stat.textContent='Row '+fmtInt(p+1)+' of '+fmtInt(g.view.length)+' shown';
};
Grid.prototype.state=function(){
  var g=this,f={},hide=[],ident=true,i;
  Object.keys(g.filt).forEach(function(c){var x=g.filt[c],y={};if(x.e)y.e=x.e;if(x.v)y.v=x.v;if(y.e||y.v)f[c]=y;});
  Object.keys(g.hide).forEach(function(c){if(g.hide[c])hide.push(Number(c));});
  hide.sort(function(a,b){return a-b;});
  for(i=0;i<g.ord.length;i++)if(g.ord[i]!==i)ident=false;
  var st={tbl:g.id,q:g.q,m:g.mo,sort:g.sort.slice(),f:f,row:drG===g?g.openRow:-1,hide:hide,ord:ident?[]:g.ord.slice(),pin:g.pin};
  var plain=!st.q&&st.m===1&&!st.sort.length&&!Object.keys(f).length&&st.row<0&&!hide.length&&ident&&!st.pin;
  return plain?{}:st;
};
Grid.prototype.applyState=function(st){
  var g=this,nf=g.nf,ok=function(c){return typeof c==='number'&&c>=0&&c<nf;};
  g.q=st.q||'';g.qh=g.q.trim().toLowerCase();g.mo=st.m===0?0:1;
  g.sort=(st.sort||[]).filter(function(s){return ok(s[0]);});
  g.filt={};Object.keys(st.f||{}).forEach(function(k){if(ok(Number(k)))g.filt[k]={e:st.f[k].e||'',v:st.f[k].v};});
  Object.keys(g.filt).forEach(function(k){if(!g.filt[k].v)delete g.filt[k].v;});
  g.hide={};(st.hide||[]).forEach(function(c){if(ok(c))g.hide[c]=1;});
  if(!g.vis().length)g.hide={};
  if(st.ord&&st.ord.length===nf){var seen={},good=true;st.ord.forEach(function(c){if(!ok(c)||seen[c])good=false;seen[c]=1;});if(good)g.ord=st.ord.slice();}
  g.pin=st.pin?1:0;g.pendingRow=typeof st.row==='number'?st.row:-1;
  g.qEl.value=g.q;g.moEl.checked=!!g.mo;
};
Grid.prototype.clearAll=function(){
  var g=this;g.q='';g.qh='';g.qEl.value='';g.filt={};g.sort=[];g.mo=1;g.moEl.checked=true;g.head();g.refilter();
};
Grid.prototype.focusSearch=function(){this.qEl.focus();this.qEl.select();};
Grid.prototype.rowsCsv=function(){
  var g=this,cols=g.vis(),out=['\ufeff'+cols.map(function(c){return csvField(g.m.fields[c]);}).join(',')];
  g.view.forEach(function(i){var r=g.rows[i];out.push(cols.map(function(c){return csvValue(r[c]);}).join(','));});
  return out.join('\r\n')+'\r\n';
};
Grid.prototype.rowsJson=function(){
  var g=this,cols=g.vis(),parts=[];
  parts.push('{"format":"sqlite-gui-analyzer-report-rows","version":1,"report":'+JSON.stringify(D.title)+',"table":'+JSON.stringify(g.m.name)+
    ',"view":'+JSON.stringify(g.describe())+',"rows_in_table":'+g.rows.length+',"rows_written":'+g.view.length+
    ',"columns":'+JSON.stringify(cols.map(function(c){return g.m.fields[c];}))+',"rows":[');
  g.view.forEach(function(i,k){var r=g.rows[i];parts.push((k?',\n':'\n')+'['+cols.map(function(c){return jsonText(r[c]);}).join(',')+']');});
  parts.push('\n]}\n');
  return parts.join('');
};
/* ------------------------------------------------------------------ popovers */
function openPop(anchor,html,kind,g,c){
  pop.innerHTML=html;pop.hidden=false;popAnchor=anchor;popKind=kind;popGrid=g||null;popCol=c===undefined?-1:c;
  var r=anchor.getBoundingClientRect(),w=pop.offsetWidth,h=pop.offsetHeight;
  var left=Math.min(Math.max(8,r.left),W.innerWidth-w-8),top=r.bottom+4;
  if(top+h>W.innerHeight-8)top=Math.max(8,r.top-h-4);
  pop.style.left=Math.max(8,left)+'px';pop.style.top=top+'px';
  var f=pop.querySelector('[data-focus]')||pop.querySelector('input,select,button');if(f)f.focus();
}
function closePop(){
  if(!pop||pop.hidden)return;
  pop.hidden=true;pop.innerHTML='';
  var a=popAnchor;popAnchor=null;popGrid=null;popCol=-1;popKind='';
  if(a&&a.isConnected)try{a.focus();}catch(e){}
}
function openCols(g,btn){
  var h=['<div class="pophead"><strong>Columns of '+esc(g.m.name)+'</strong><div class="muted small">Show, hide and order the columns.</div></div><ul>'];
  g.ord.forEach(function(c,k){
    var nm=esc(g.m.fields[c]);
    h.push('<li><label><input type="checkbox" data-p="show" data-c="'+c+'"'+(g.hide[c]?'':' checked')+'> '+nm+'</label><span>'+
      '<button type="button" class="btn sm" data-p="up" data-c="'+c+'" aria-label="Move '+nm+' earlier"'+(k===0?' disabled':'')+'>\u25b2</button> '+
      '<button type="button" class="btn sm" data-p="down" data-c="'+c+'" aria-label="Move '+nm+' later"'+(k===g.ord.length-1?' disabled':'')+'>\u25bc</button></span></li>');
  });
  h.push('</ul><label class="chk"><input type="checkbox" data-p="pin"'+(g.pin?' checked':'')+'> Pin the first column</label>');
  h.push('<div class="row"><button type="button" class="btn" data-p="all">Show all</button><button type="button" class="btn" data-p="reset">Reset columns</button></div>');
  openPop(btn,h.join(''),'cols',g);
}
function openTz(btn){
  var cur=curTz(),lo=-new Date().getTimezoneOffset(),h=['<div class="pophead"><strong>Show dates in</strong><div class="muted small">Every date column of the report; the raw values never change. Filters on dates read typed dates in this zone too.</div></div>'];
  h.push('<input type="search" id="tz-q" placeholder="Find a zone or place" data-focus aria-label="Find a time zone"><ul class="tzl">');
  var item=function(val,label,on){return '<li data-t="'+esc(label.toLowerCase())+'"><button type="button" class="btn sm tzb'+(on?' on':'')+'" data-p="tz" data-z="'+val+'" aria-pressed="'+(on?'true':'false')+'">'+esc(label)+'</button></li>';};
  h.push(item('utc','UTC',cur===0));
  h.push(item('local','Local time of this computer ('+tzName()+(ZONES[String(lo)]&&lo?' \u00b7 '+ZONES[String(lo)]:'')+')',cur==='local'));
  Object.keys(ZONES).map(Number).sort(function(a,b){return a-b;}).forEach(function(m){if(m)h.push(item(String(m),zoneText(m),cur===m));});
  h.push('</ul>');
  openPop(btn,h.join(''),'tz',null);
  $('tz-q').addEventListener('input',function(){var q=this.value.toLowerCase();[].forEach.call(pop.querySelectorAll('.tzl li'),function(li){li.hidden=q&&li.getAttribute('data-t').indexOf(q)<0;});});
}
function setTz(z){
  prefs.tz=z==='local'?'local':(z==='utc'?'utc':Number(z));savePrefs();updateToggles();
  grids.forEach(function(g){if(g.ready){g.rt=null;g.head();g.refilter();}});if(drG)renderDrawer();
  say('Dates shown in '+tzText());
}
function openDl(g,btn){
  var n=g.view?g.view.length:0,d=g.describe();
  openPop(btn,'<div class="pophead"><strong>Download the rows shown</strong><div class="muted small">'+fmtInt(n)+' row'+(n===1?'':'s')+
    (d?' ('+esc(d)+')':'')+', the columns shown, values as in the tool\'s CSV / JSON exports. Made in your browser; nothing is sent anywhere.</div></div>'+
    '<div class="row"><button type="button" class="btn" data-p="csv" data-focus>CSV</button><button type="button" class="btn" data-p="json">JSON</button></div>','dl',g);
}
var CONDS=[['contains','contains'],['ncontains','does not contain'],['eq','equals'],['ne','does not equal'],['starts','starts with'],['ends','ends with'],
  ['gt','greater than / after'],['ge','at least'],['lt','less than / before'],['le','at most'],['between','between (inclusive)'],['empty','is empty'],['nempty','is not empty'],
  ['null','is NULL'],['nnull','is not NULL'],['re','matches the regular expression']];
function openFilter(g,c,btn){
  var m=g.m,name=m.fields[c],cur=g.filt[c]||{},di=g.dinfo[c],counts=Object.create(null),keys=[],rows=g.rows,i,k;
  for(i=0;i<rows.length;i++){k=cellKey(rows[i][c]);if(counts[k]===undefined){counts[k]=0;keys.push(k);}counts[k]++;}
  keys.sort(function(a,b){return counts[b]-counts[a]||(a<b?-1:(a>b?1:0));});
  var cap=m.distinct||1000,shown=keys.slice(0,cap),sel=null;
  if(cur.v){sel=Object.create(null);cur.v.forEach(function(x){sel[x]=1;});}
  var h=['<div class="pophead"><strong>'+esc(name)+'</strong>'+(di?' <span class="muted small">dates read as '+esc(di[2])+'; shown in '+esc(tzText())+'</span>':'')+'</div>'];
  h.push('<div class="row"><label class="vh" for="pc-op">Condition</label><select id="pc-op" data-focus>'+CONDS.map(function(o){return '<option value="'+o[0]+'">'+esc(o[1])+'</option>';}).join('')+'</select>');
  h.push('<label class="vh" for="pc-a">Value</label><input type="text" id="pc-a" placeholder="'+(di?'YYYY-MM-DD HH:MM':'value')+'"><label class="vh" for="pc-b">And</label><input type="text" id="pc-b" placeholder="and" hidden>');
  h.push('<button type="button" class="btn" data-p="cond">Apply</button></div>');
  h.push('<div class="muted small">Writes the expression into the filter box, where it can be edited (press ? for the syntax).</div>');
  h.push('<div class="popsec"><div><strong>Values</strong> <span class="muted small">'+fmtInt(keys.length)+' distinct'+(keys.length>cap?'; the '+fmtInt(cap)+' most frequent are listed (limit filter_distinct_values): values not listed are left out when a choice is applied':'')+'</span></div>');
  h.push('<div class="row"><label class="vh" for="pv-q">Find a value</label><input type="search" id="pv-q" placeholder="Find a value"><button type="button" class="btn sm" data-p="vall">All</button><button type="button" class="btn sm" data-p="vnone">None</button></div><ul class="pvl">');
  shown.forEach(function(key,ix){
    var label=key==='\u0000NULL'?'NULL':key,t=label.length>120?label.slice(0,120)+'\u2026':label;
    h.push('<li data-t="'+esc(label.toLowerCase().slice(0,300))+'"><label title="'+esc(label.slice(0,1000))+'"><input type="checkbox" data-ix="'+ix+'"'+(!sel||sel[key]?' checked':'')+'> '+(key==='\u0000NULL'?'<span class="nul">NULL</span>':esc(t))+'</label><span class="cnt">'+fmtInt(counts[key])+'</span></li>');
  });
  h.push('</ul><div class="row"><button type="button" class="btn" data-p="vals">Apply the values chosen</button></div></div>');
  h.push('<div class="popsec row"><button type="button" class="btn sm" data-p="fclear">Clear this filter</button><span class="muted small">Width</span>'+
    '<button type="button" class="btn sm" data-p="narrow" aria-label="Narrower">\u2212</button><button type="button" class="btn sm" data-p="wide" aria-label="Wider">+</button>'+
    '<button type="button" class="btn sm" data-p="fit" title="As wide as the values need">Fit</button>'+
    '<button type="button" class="btn sm" data-p="hidecol">Hide this column</button></div>');
  openPop(btn,h.join(''),'filter',g,c);
  pop.keys=shown;pop.allKeys=keys.length;
  var op=$('pc-op'),b=$('pc-b');
  op.addEventListener('change',function(){var v=op.value;b.hidden=v!=='between';$('pc-a').hidden=/^(empty|nempty|null|nnull)$/.test(v);});
  $('pv-q').addEventListener('input',function(){var q=this.value.toLowerCase();[].forEach.call(pop.querySelectorAll('.pvl li'),function(li){li.hidden=q&&li.getAttribute('data-t').indexOf(q)<0;});});
}
function popAction(a,el){
  var g=popGrid,c=popCol,ci=el&&el.getAttribute('data-c')!==null?Number(el.getAttribute('data-c')):-1,k;
  if(popKind==='tz'){if(a==='tz'){closePop();setTz(el.getAttribute('data-z'));}return;}
  if(!g)return;
  if(popKind==='cols'){
    if(a==='show'){g.hide[ci]=el.checked?0:1;if(!g.vis().length){g.hide[ci]=0;el.checked=true;say('At least one column stays shown');}}
    else if(a==='up'||a==='down'){k=g.ord.indexOf(ci);var j=a==='up'?k-1:k+1;if(j<0||j>=g.ord.length)return;g.ord[k]=g.ord[j];g.ord[j]=ci;}
    else if(a==='pin')g.pin=el.checked?1:0;
    else if(a==='all')g.hide={};
    else if(a==='reset'){g.hide={};g.pin=0;g.ord=[];for(k=0;k<g.nf;k++)g.ord.push(k);g.w=(g.m.widths||[]).slice();for(k=0;k<g.nf;k++)if(!g.w[k])g.w[k]=140;}
    g.head();g.measured=false;g.paint();scheduleHash(g);
    if(a!=='show'&&a!=='pin'){var anchor=popAnchor;openCols(g,anchor);var f=pop.querySelector('[data-p="'+a+'"][data-c="'+ci+'"]');if(f&&!f.disabled)f.focus();}
    return;
  }
  if(popKind==='dl'){
    if(a==='csv')download(safeName(g.m.name)+'.csv',g.rowsCsv(),'text/csv;charset=utf-8');
    else if(a==='json')download(safeName(g.m.name)+'.json',g.rowsJson(),'application/json');
    closePop();return;
  }
  if(popKind==='filter'){
    var f=g.filt[c]||{};
    if(a==='cond'){
      var e=condExpr($('pc-op').value,$('pc-a').value,$('pc-b').value);
      f.e=e;g.filt[c]=f;closePop();g.head();g.refilter();scheduleHash(g);
    }else if(a==='vall'||a==='vnone'){
      [].forEach.call(pop.querySelectorAll('.pvl li'),function(li){if(!li.hidden)li.querySelector('input').checked=a==='vall';});
    }else if(a==='vals'){
      var chosen=[],all=true;
      [].forEach.call(pop.querySelectorAll('.pvl input[data-ix]'),function(i){if(i.checked)chosen.push(pop.keys[Number(i.getAttribute('data-ix'))]);else all=false;});
      if(all&&pop.allKeys<=pop.keys.length)delete f.v;else f.v=chosen;
      if(f.e||f.v)g.filt[c]=f;else delete g.filt[c];
      closePop();g.head();g.refilter();scheduleHash(g);
    }else if(a==='fclear'){delete g.filt[c];closePop();g.head();g.refilter();scheduleHash(g);}
    else if(a==='narrow'||a==='wide'){g.w[c]=Math.max(48,Math.min(1200,g.w[c]+(a==='wide'?40:-40)));g.head();g.paint();}
    else if(a==='fit')g.fit(c);
    else if(a==='hidecol'){g.hide[c]=1;if(!g.vis().length)g.hide[c]=0;closePop();g.head();g.paint();scheduleHash(g);}
  }
}
/* ------------------------------------------------------------------ the row drawer */
function openDrawer(g,p,focus){
  if(!g.view||p<0||p>=g.view.length)return;
  drG=g;drP=p;g.act=p;g.openRow=g.view[p];
  renderDrawer();dr.hidden=false;g.paint();scheduleHash(g);
  if(focus!==false)try{drTitle.focus();}catch(e){}
}
function closeDrawer(){
  if(!dr||dr.hidden)return;
  dr.hidden=true;var g=drG;drG=null;drP=-1;
  if(g){g.openRow=-1;scheduleHash(g);try{g.wrap.focus({preventScroll:true});}catch(e){}}
}
function moveDrawer(d){
  if(!drG)return;var p=drP+d;if(p<0||p>=drG.view.length)return;
  drG.move(d);
}
function valHtml(g,c,r){
  var v=r[c],di=g.dix[c],h='',s,b;
  if(isNull(v))return '<span class="nul">NULL</span>';
  if(di!==undefined&&typeof r[di]==='number'){
    h+='<div class="dts">'+(typeof curTz()==='number'&&curTz()?'<span><strong>'+esc(fmtDate(r[di],curTz()))+'</strong> '+esc(zoneText(curTz()))+'</span>':'')+
      '<span>'+esc(fmtDate(r[di],false))+' UTC</span><span>'+esc(fmtDate(r[di],true))+' local time ('+tzName()+')</span><span class="muted small">read as '+esc(g.dinfo[c][2])+'; the raw value:</span></div>';
  }
  if(isObj(v)&&v.b!==undefined){
    b=v.b;
    h+='<div><strong>BLOB</strong> '+fmtInt(b.n)+' bytes'+(b.s?' \u00b7 '+esc(b.s):'')+
      ' <button type="button" class="btn sm" data-act="d-blob" data-c="'+c+'" title="Decoded value, hex and the text inside, with Find and Save">Inspect\u2026</button></div>';
    if(b.dv!==undefined){
      h+='<details class="dec" open><summary>Decoded ('+esc(b.dk||'')+')</summary><pre class="val mono">'+esc(b.dv)+'</pre>'+
        (b.dc?'<div class="muted small">The first '+fmtInt(b.dv.length)+' of '+fmtInt(b.dc)+' characters (limit html_decoded_chars)</div>':'')+'</details>';
    }
    if(b.h)h+='<div class="mono">SHA-256 '+esc(b.h)+'</div>';
    if(b.p)h+='<div class="muted small">Only the first '+fmtInt(b.p.kept)+' bytes are kept; SHA-256 of the whole value '+esc(b.p.h)+'</div>';
    if(b.m){
      if(b.big)h+='<div class="muted small">An image ('+esc(b.m)+'), not shown: '+fmtInt(b.n)+' bytes is more than the limit html_thumb_bytes ('+fmtInt(g.m.thumb)+' bytes).</div>';
      else h+='<div class="dthumb" data-c="'+c+'"></div>';
    }
    if(b.hex!==undefined){
      h+='<details><summary>Bytes (hex'+(b.n>4096?', the first 4,096 of '+fmtInt(b.n):'')+')</summary><pre class="val mono">'+esc(hexDump(b.hex,4096))+'</pre></details>';
    }else if(b.b64!==undefined)h+='<details><summary>Bytes (base64)</summary><pre class="val mono">'+esc(b.b64.length>20000?b.b64.slice(0,20000)+'\u2026':b.b64)+'</pre></details>';
    return h;
  }
  if(isObj(v)&&v.x!==undefined)return h+'<div class="bad">Invalid text ('+fmtInt(v.x.length/2)+' bytes): the valid parts as text, each invalid byte as \\xNN</div><pre class="val">'+esc(v.t)+'</pre><div class="mono small">hex '+esc(v.x)+'</div>';
  if(isObj(v)&&v.m!==undefined)return '<span class="muted">not in this row</span>';
  if(isObj(v)&&v.j!==undefined)return h+'<pre class="val">'+esc(JSON.stringify(plainJ(v.j),null,1))+'</pre>';
  s=cellText(v);
  if(typeof v!=='string')return h+'<span class="mono">'+esc(s)+'</span>';
  return h+'<pre class="val">'+esc(s)+'</pre>'+(s.length>200?'<div class="muted small">'+fmtInt(s.length)+' characters</div>':'');
}
function renderDrawer(){
  var g=drG;if(!g)return;
  var m=g.m,i=g.view[drP],r=g.rows[i],h=[],det=m.detail||{};
  drTitle.textContent=m.name+' \u00b7 row '+fmtInt(i+g.first);
  drPos.textContent='Row '+fmtInt(drP+1)+' of '+fmtInt(g.view.length)+' shown'+(g.view.length<g.rows.length?' (of '+fmtInt(g.rows.length)+')':'');
  if(det.path&&det.path.length)h.push('<p class="dpath">'+det.path.map(function(x){return esc(typeof x==='number'?cellText(r[x]):x);}).join(' <span aria-hidden="true">\u203a</span> ')+'</p>');
  if(g.tagc>=0||typeof m.notes==='number'){
    h.push('<h3>Tags and note</h3>');
    if(g.tagc>=0)h.push('<div>'+(chipsHtml(g,r[g.tagc])||'<span class="muted">no tag</span>')+'</div>');
    if(typeof m.notes==='number'&&!isNull(r[m.notes])&&cellText(r[m.notes]))h.push('<pre class="val">'+esc(cellText(r[m.notes]))+'</pre>');
  }
  var shownProv={};
  if(det.prov&&det.prov.length){
    h.push('<h3>Provenance</h3><dl class="kv">');
    det.prov.forEach(function(c){shownProv[c]=1;h.push('<dt>'+esc(m.fields[c])+'</dt><dd>'+valHtml(g,c,r)+'</dd>');});
    h.push('</dl>');
  }
  h.push('<h3>Values</h3><dl class="kv">');
  for(var c=0;c<g.nf;c++){
    if(shownProv[c]||c===g.tagc)continue;
    h.push('<dt>'+esc(m.fields[c])+'<button type="button" class="btn sm cp" data-act="d-copy" data-c="'+c+'" aria-label="Copy '+esc(m.fields[c])+'">Copy</button></dt><dd>'+valHtml(g,c,r)+'</dd>');
  }
  h.push('</dl>');
  drBody.innerHTML=h.join('');
  [].forEach.call(drBody.querySelectorAll('.dthumb[data-c]'),function(el){
    var b=r[Number(el.getAttribute('data-c'))].b,data=b.img||b.b64||(b.hex!==undefined?hexToB64(b.hex):'');
    if(!data||!/^image\/(png|jpeg|gif|webp)$/.test(b.m))return;
    var im=D.createElement('img');im.className='thumb';im.alt='The image in this value ('+fmtInt(b.n)+' bytes)';im.src='data:'+b.m+';base64,'+data;el.appendChild(im);
  });
  drBody.scrollTop=0;
}
function rowJson(g,r){return '{'+g.m.fields.map(function(nm,c){return JSON.stringify(nm)+':'+jsonText(r[c]);}).join(',')+'}';}
function rowTsv(g,r){
  var f=function(s){s=String(s);return /[\t"\r\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;};
  return g.m.fields.map(f).join('\t')+'\n'+g.m.fields.map(function(nm,c){return f(csvCell(r[c]));}).join('\t')+'\n';
}
/* ------------------------------------------------------------------ the BLOB inspector */
/* One BLOB of a row: its decoded value, its bytes as hex (4 KB a page) and the text inside
   them, with one find box over all three (text as UTF-8 and UTF-16, or hex as 0x0a1b /
   0a 1b) and Save BLOB. Everything is read from this file. */
var bi=null,biS=null,BI_PAGE=4096,BI_HITS=10000,BI_STRINGS=5000;
function blobBytes(b){
  var a,i,s;
  if(b.hex!==undefined){a=new Uint8Array(b.hex.length>>1);for(i=0;i<a.length;i++)a[i]=parseInt(b.hex.substr(2*i,2),16);return a;}
  if(b.b64!==undefined){s=W.atob(b.b64);a=new Uint8Array(s.length);for(i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a;}
  return null;
}
function openBlob(g,c,r){
  var b=r[c].b,data=blobBytes(b),row=g.view[drP]+g.first;
  biS={b:b,data:data,name:g.m.name+'.'+g.m.fields[c]+' \u00b7 row '+fmtInt(row),file:safeName(g.m.name+'_row'+row+'_'+g.m.fields[c]),
       tab:b.dv!==undefined?'dec':'hex',page:0,hits:[],dn:0,hi:-1,q:'',back:D.activeElement};
  bi.hidden=false;renderBlob();
  var q=$('bi-q');if(q)q.focus();
}
function closeBlob(){
  if(!bi||bi.hidden)return;
  bi.hidden=true;var b=biS&&biS.back;biS=null;
  if(b&&b.focus)try{b.focus();}catch(e){}
}
function hexPattern(q){
  var m=/^\s*(?:0x|x')?((?:[0-9a-fA-F]{2}[\s:]*)+)'?\s*$/.exec(q);
  if(!m||!(/^\s*(0x|x')/.test(q)||/[0-9a-fA-F]{2}[\s:]+[0-9a-fA-F]{2}/.test(q)))return null;
  var h=m[1].replace(/[\s:]/g,''),a=[],i;
  for(i=0;i+1<h.length;i+=2)a.push(parseInt(h.substr(i,2),16));
  return a;
}
function findBytes(data,pat,fold,out,kind){
  var n=data.length,m=pat.length,i,j,x;
  if(!m)return;
  for(i=0;i+m<=n&&out.length<BI_HITS;i++){
    for(j=0;j<m;j++){x=data[i+j];if(fold&&x>=65&&x<=90)x+=32;if(x!==pat[j])break;}
    if(j===m)out.push({o:i,n:m,k:kind});
  }
}
function biFind(q){
  var s=biS;if(!s)return;
  s.q=q;s.hits=[];s.hi=-1;s.dn=0;
  var t=String(q).trim();
  if(t){
    var hp=hexPattern(t);
    if(s.data){
      if(hp)findBytes(s.data,hp,false,s.hits,'hex');
      else{
        var u8=unescape(encodeURIComponent(t.toLowerCase())),p8=[],p16=[],i,ascii=true;
        for(i=0;i<u8.length;i++)p8.push(u8.charCodeAt(i));
        for(i=0;i<t.length;i++){var cc=t.toLowerCase().charCodeAt(i);if(cc>255)ascii=false;p16.push(cc&255);p16.push(cc>>8);}
        findBytes(s.data,p8,true,s.hits,'UTF-8');
        if(ascii&&t.length>1)findBytes(s.data,p16,true,s.hits,'UTF-16');
        s.hits.sort(function(a,b){return a.o-b.o;});
      }
    }
    if(s.b.dv!==undefined&&!hp){var l=s.b.dv.toLowerCase(),k=l.indexOf(t.toLowerCase());while(k>=0&&s.dn<BI_HITS){s.dn++;k=l.indexOf(t.toLowerCase(),k+1);}}
  }
  biStatus();biBody();
}
function biStatus(){
  var s=biS,el=$('bi-n');if(!el)return;
  if(!String(s.q).trim()){el.textContent='';return;}
  var by={};s.hits.forEach(function(h){by[h.k]=(by[h.k]||0)+1;});
  var parts=[];
  if(s.data)parts.push(fmtInt(s.hits.length)+(s.hits.length>=BI_HITS?'+':'')+' in the bytes'+(Object.keys(by).length?' ('+Object.keys(by).map(function(k){return k+' '+fmtInt(by[k]);}).join(', ')+')':''));
  if(s.b.dv!==undefined)parts.push(fmtInt(s.dn)+' in the decoded value');
  el.textContent=parts.join(' \u00b7 ')+(s.hi>=0?' \u00b7 at offset 0x'+s.hits[s.hi].o.toString(16)+' ('+fmtInt(s.hits[s.hi].o)+')':'');
}
function biNext(d){
  var s=biS;if(!s)return;
  if(s.tab==='dec'){
    var ms=bi.querySelectorAll('#bi-body mark');if(!ms.length)return;
    s.di=((s.di===undefined?-1:s.di)+d+ms.length)%ms.length;
    [].forEach.call(ms,function(m,i){m.classList.toggle('cur',i===s.di);});
    ms[s.di].scrollIntoView({block:'center'});return;
  }
  if(!s.hits.length)return;
  s.hi=(s.hi+d+s.hits.length) % (s.hits.length);s.page=Math.floor(s.hits[s.hi].o/BI_PAGE);
  if(s.tab!=='hex')s.tab='hex';
  renderBlob(true);
  var cur=bi.querySelector('#bi-body .cur');if(cur)cur.scrollIntoView({block:'center'});
}
function renderBlob(keepFocus){
  var s=biS,b=s.b,h=[],tabs=[];
  if(b.dv!==undefined)tabs.push(['dec','Decoded ('+(b.dk||'')+')']);
  if(s.data){tabs.push(['hex','Hex']);tabs.push(['str','Text inside']);}
  if(!tabs.length)tabs.push(['dec','Summary']);
  h.push('<div class="bihead"><h2 id="bi-title">BLOB</h2><button type="button" class="btn sm" data-act="bi-close">Close (Esc)</button></div>');
  h.push('<p class="muted small">'+esc(s.name)+' \u00b7 '+fmtInt(b.n)+' bytes'+(b.s?' \u00b7 '+esc(b.s):'')+'</p>');
  if(b.p)h.push('<p class="note">Only the first '+fmtInt(b.p.kept)+' bytes are in this report; SHA-256 of the whole value '+esc(b.p.h)+'.</p>');
  if(!s.data)h.push('<p class="note">This report keeps a summary of each BLOB, not its bytes'+(b.h?' (SHA-256 '+esc(b.h)+')':'')+'. Export it again with BLOB values as hex or base64 to see and search the bytes here.</p>');
  h.push('<div class="bitabs" role="tablist">'+tabs.map(function(t){return '<button type="button" role="tab" class="btn sm'+(s.tab===t[0]?' on':'')+'" aria-selected="'+(s.tab===t[0]?'true':'false')+'" data-act="bi-tab" data-t="'+t[0]+'">'+esc(t[1])+'</button>';}).join('')+
    (s.data?'<button type="button" class="btn sm" data-act="bi-save">Save BLOB\u2026</button>':'')+'</div>');
  h.push('<div class="row bifind"><label class="vh" for="bi-q">Find in this BLOB</label><input type="search" id="bi-q" placeholder="Find text, or hex as 0x0a1b or 0a 1b" autocomplete="off" spellcheck="false" value="'+esc(s.q)+'">'+
    '<button type="button" class="btn sm" data-act="bi-prev" aria-label="Previous match">\u2191</button><button type="button" class="btn sm" data-act="bi-next" aria-label="Next match">\u2193</button><span id="bi-n" class="muted small" role="status"></span></div>');
  h.push('<div id="bi-body"></div>');
  bi.firstChild.innerHTML=h.join('');
  var q=$('bi-q');
  q.addEventListener('input',debounce(function(){biFind(q.value);},200));
  q.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();biNext(e.shiftKey?-1:1);}});
  if(keepFocus)try{q.focus({preventScroll:true});}catch(e){}
  biStatus();biBody();
}
function hlAll(s,q){
  s=String(s);if(!q)return esc(s);
  var l=s.toLowerCase(),i=l.indexOf(q),o='',j=0,n=0;
  while(i>=0&&n<BI_HITS){o+=esc(s.slice(j,i))+'<mark>'+esc(s.slice(i,i+q.length))+'</mark>';j=i+q.length;i=l.indexOf(q,j);n++;}
  return o+esc(s.slice(j));
}
function blobStrings(data,q){
  var out=[],n=data.length,i=0,st,t,more=false;
  while(i<n){
    if(data[i]>=32&&data[i]<127){st=i;while(i<n&&data[i]>=32&&data[i]<127)i++;if(i-st>=4){t=String.fromCharCode.apply(null,data.subarray(st,Math.min(i,st+400)));out.push([st,'ASCII',t]);}continue;}
    if(i+1<n&&data[i]>=32&&data[i]<127&&data[i+1]===0){st=i;t='';while(i+1<n&&data[i]>=32&&data[i]<127&&data[i+1]===0&&t.length<400){t+=String.fromCharCode(data[i]);i+=2;}if(t.length>=4)out.push([st,'UTF-16',t]);continue;}
    i++;
  }
  /* UTF-16 runs start on a printable byte followed by 0: look for them too */
  for(i=0;i+1<n;i++){
    if(data[i]>=32&&data[i]<127&&data[i+1]===0&&(i<2||data[i-1]!==0||!(data[i-2]>=32&&data[i-2]<127))){
      st=i;t='';var k=i;while(k+1<n&&data[k]>=32&&data[k]<127&&data[k+1]===0&&t.length<400){t+=String.fromCharCode(data[k]);k+=2;}
      if(t.length>=4)out.push([st,'UTF-16',t]);i=k;
    }
  }
  out.sort(function(a,b){return a[0]-b[0];});
  if(q)out=out.filter(function(x){return x[2].toLowerCase().indexOf(q)>=0;});
  if(out.length>BI_STRINGS){out=out.slice(0,BI_STRINGS);more=true;}
  return {list:out,more:more};
}
function biBody(){
  var s=biS,b=s.b,body=$('bi-body'),q=String(s.q).trim().toLowerCase(),h=[],i;
  if(!body)return;
  if(s.tab==='dec'){
    if(b.dv===undefined){body.innerHTML='<p class="muted">'+esc(b.s||'No decoded value in this report.')+'</p>';return;}
    h.push('<pre class="val mono bipre">'+hlAll(b.dv,hexPattern(q)?'':q)+'</pre>');
    if(b.dc)h.push('<p class="muted small">The first '+fmtInt(b.dv.length)+' of '+fmtInt(b.dc)+' characters (limit html_decoded_chars).</p>');
    s.di=undefined;body.innerHTML=h.join('');return;
  }
  if(!s.data){body.innerHTML='';return;}
  if(s.tab==='str'){
    var r=blobStrings(s.data,q);
    if(!r.list.length){body.innerHTML='<p class="muted">'+(q?'No text inside the bytes holds this.':'No run of 4 or more readable characters in the bytes.')+'</p>';return;}
    h.push('<div class="bihex">');
    r.list.forEach(function(x){h.push('<a href="#" class="bioff" data-act="bi-go" data-o="'+x[0]+'">'+pad(x[0].toString(16),8)+'</a>  '+(x[1]==='UTF-16'?'<span class="muted">u16</span> ':'    ')+hlAll(x[2],q)+'\n');});
    h.push('</div>');
    if(r.more)h.push('<p class="muted small">The first '+fmtInt(BI_STRINGS)+' texts are listed.</p>');
    body.innerHTML=h.join('');return;
  }
  var n=s.data.length,pages=Math.max(1,Math.ceil(n/BI_PAGE)),pg=Math.min(Math.max(0,s.page),pages-1),start=pg*BI_PAGE,end=Math.min(n,start+BI_PAGE),mark=new Uint8Array(end-start),cur=s.hi>=0?s.hits[s.hi]:null;
  s.hits.forEach(function(x){for(var k=Math.max(x.o,start);k<Math.min(x.o+x.n,end);k++)mark[k-start]=x===cur?2:1;});
  h.push('<div class="row binav"><button type="button" class="btn sm" data-act="bi-page" data-d="-1"'+(pg?'':' disabled')+'>\u25c0 Previous 4 KB</button>'+
    '<span class="muted small">bytes '+fmtInt(start)+'\u2013'+fmtInt(Math.max(start,end-1))+' of '+fmtInt(n)+' (page '+fmtInt(pg+1)+' of '+fmtInt(pages)+')</span>'+
    '<button type="button" class="btn sm" data-act="bi-page" data-d="1"'+(pg<pages-1?'':' disabled')+'>Next 4 KB \u25b6</button>'+
    '<label class="vh" for="bi-off">Go to offset</label><input type="text" id="bi-off" placeholder="offset: 0x1f or 31" size="14"><button type="button" class="btn sm" data-act="bi-goto">Go</button></div>');
  h.push('<div class="bihex">');
  var span=function(k,txt){return mark[k-start]?'<mark'+(mark[k-start]===2?' class="cur"':'')+'>'+txt+'</mark>':txt;};
  for(i=start;i<end;i+=16){
    var hx='',asc='',j,v;
    for(j=i;j<i+16;j++){
      if(j<end){v=s.data[j];hx+=span(j,(v<16?'0':'')+v.toString(16))+' ';asc+=span(j,v>=32&&v<127?esc(String.fromCharCode(v)):'.');}
      else hx+='   ';
      if(j===i+7)hx+=' ';
    }
    h.push('<span class="muted">'+pad(i.toString(16),8)+'</span>  '+hx+' '+asc+'\n');
  }
  h.push('</div>');
  body.innerHTML=h.join('');
  var off=$('bi-off');if(off)off.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();biGoto(off.value);}});
}
function biGoto(v){
  var s=biS,t=String(v).trim().toLowerCase(),o=/^(0x[0-9a-f]+|[0-9a-f]+h)$/.test(t)?parseInt(t.replace(/^0x|h$/g,''),16):parseInt(t,10);
  if(isNaN(o)||o<0||!s.data||o>=s.data.length){say('No such offset in this BLOB');return;}
  s.tab='hex';s.page=Math.floor(o/BI_PAGE);s.hits=s.hits.filter(function(x){return x.k!=='go';});s.hits.push({o:o,n:1,k:'go'});s.hi=s.hits.length-1;
  renderBlob(true);var c=bi.querySelector('#bi-body .cur');if(c)c.scrollIntoView({block:'center'});
}
function blobAction(a,el,e){
  var s=biS;if(!s)return;
  if(a==='bi-close')closeBlob();
  else if(a==='bi-tab'){s.tab=el.getAttribute('data-t');renderBlob(true);}
  else if(a==='bi-next')biNext(1);
  else if(a==='bi-prev')biNext(-1);
  else if(a==='bi-page'){s.page+=Number(el.getAttribute('data-d'));biBody();}
  else if(a==='bi-goto'){var off=$('bi-off');if(off)biGoto(off.value);}
  else if(a==='bi-go'){if(e)e.preventDefault();biGoto(el.getAttribute('data-o'));}
  else if(a==='bi-save'&&s.data){
    var blob=new Blob([s.data],{type:'application/octet-stream'}),url=URL.createObjectURL(blob),ln=D.createElement('a');
    ln.href=url;ln.download=s.file+'.bin';D.body.appendChild(ln);ln.click();
    setTimeout(function(){URL.revokeObjectURL(url);if(ln.parentNode)ln.parentNode.removeChild(ln);},4000);
    say('Download started: '+s.file+'.bin');
  }
}
/* ------------------------------------------------------------------ table of contents, hash */
function tocItem(id){
  var all=D.querySelectorAll('#toc li[data-sec]');
  for(var i=0;i<all.length;i++)if(all[i].getAttribute('data-sec')===id)return all[i];
  return null;
}
function tocCount(g){
  var it=tocItem(g.id),li=it&&it.querySelector('.cnt');if(!li)return;
  var n=g.rows.length,v=g.view?g.view.length:n;
  li.textContent=v<n?fmtInt(v)+' of '+fmtInt(n):fmtInt(n);
}
function scheduleHash(g){if(g)hashGrid=g;clearTimeout(hashT);hashT=setTimeout(writeHash,200);}
function writeHash(){
  if(!live)return;
  var st=hashGrid?hashGrid.state():{};st.sec=curSec;
  var h='#'+stEnc(st);
  if(h!==W.location.hash)try{W.history.replaceState(null,'',h);}catch(e){}
}
function setCur(id){
  if(id===curSec)return;curSec=id;
  [].forEach.call(D.querySelectorAll('#toc a[aria-current]'),function(a){a.removeAttribute('aria-current');});
  var it=tocItem(id),a=it&&it.querySelector(':scope>a');if(a)a.setAttribute('aria-current','true');
  scheduleHash(null);
}
function setupSpy(){
  if(!W.IntersectionObserver)return;
  var seen={};
  var io=new W.IntersectionObserver(function(es){
    es.forEach(function(e){seen[e.target.id]=e.isIntersecting?e.boundingClientRect.top:null;});
    var best=null,bt=-Infinity;
    Object.keys(seen).forEach(function(id){var t=seen[id];if(t!==null&&t>bt){bt=t;best=id;}});
    if(best)setCur(best);
  },{rootMargin:'-60px 0px -55% 0px'});
  [].forEach.call(D.querySelectorAll('[data-toc]'),function(el){io.observe(el);});
}
function activeGrid(){
  if(lastGrid&&lastGrid.ready){var r=lastGrid.el.getBoundingClientRect();if(r.bottom>0&&r.top<W.innerHeight)return lastGrid;}
  for(var i=0;i<grids.length;i++){var g=grids[i];if(!g.ready)continue;var b=g.el.getBoundingClientRect();if(b.bottom>60&&b.top<W.innerHeight)return g;}
  return lastGrid&&lastGrid.ready?lastGrid:null;
}
/* ------------------------------------------------------------------ report search */
var rsMarks=[],rsCur=-1,rsGen=0;
function clearMarks(){
  rsMarks.forEach(function(mk){var p=mk.parentNode;if(!p)return;p.replaceChild(D.createTextNode(mk.textContent),mk);p.normalize();});
  rsMarks=[];rsCur=-1;
}
function reportSearch(q){
  var gen=++rsGen,ql=q.trim().toLowerCase(),by={},total=0,capped=false,stat=$('rs-stat');
  clearMarks();
  var items=[].slice.call(D.querySelectorAll('#toc li[data-sec]'));
  items.forEach(function(li){li.classList.remove('rs-hide');var hs=li.querySelector(':scope>a .hits');if(hs)hs.parentNode.removeChild(hs);});
  if(!ql){stat.textContent='';return;}
  var nodes=[],n,walker=D.createTreeWalker($('main'),W.NodeFilter.SHOW_TEXT,{acceptNode:function(nd){
    var p=nd.parentNode;
    if(!p||!nd.nodeValue||nd.nodeValue.toLowerCase().indexOf(ql)<0)return W.NodeFilter.FILTER_REJECT;
    if(p.closest&&p.closest('script,style,svg,.grid,.static-wrap,noscript,.print-only'))return W.NodeFilter.FILTER_REJECT;
    return W.NodeFilter.FILTER_ACCEPT;}});
  while((n=walker.nextNode()))nodes.push(n);
  nodes.forEach(function(nd){
    var sec=nd.parentNode.closest('[data-toc]'),id=sec?sec.id:'',text=nd.nodeValue,low=text.toLowerCase(),i=low.indexOf(ql),j=0,frag=D.createDocumentFragment();
    while(i>=0){
      total++;by[id]=(by[id]||0)+1;
      if(rsMarks.length<1000){frag.appendChild(D.createTextNode(text.slice(j,i)));var mk=D.createElement('mark');mk.className='rsm';mk.textContent=text.slice(i,i+ql.length);frag.appendChild(mk);rsMarks.push(mk);j=i+ql.length;}
      else capped=true;
      i=low.indexOf(ql,i+ql.length);
    }
    frag.appendChild(D.createTextNode(text.slice(j)));nd.parentNode.replaceChild(frag,nd);
  });
  var rowsBy={},todo=grids.filter(function(g){return g.ready;}),gi=0,ri=0,cnt=0;
  function show(){
    var secs=0;
    items.slice().reverse().forEach(function(li){
      var id=li.getAttribute('data-sec'),own=(by[id]||0)+(rowsBy[id]||0),kids=li.querySelectorAll('li[data-sec]:not(.rs-hide)').length;
      if(!own&&!kids)li.classList.add('rs-hide');else li.classList.remove('rs-hide');
      var a=li.querySelector(':scope>a'),hs=a&&a.querySelector('.hits');
      if(own){secs++;if(!hs&&a){hs=D.createElement('span');hs.className='hits';a.appendChild(hs);}if(hs)hs.textContent=rowsBy[id]?fmtInt(rowsBy[id])+' row'+(rowsBy[id]===1?'':'s'):fmtInt(own);}
      else if(hs)hs.parentNode.removeChild(hs);
    });
    var rows=0;Object.keys(rowsBy).forEach(function(k){rows+=rowsBy[k];});
    stat.textContent=fmtInt(total)+' match'+(total===1?'':'es')+' in the text'+(capped?' (the first 1,000 highlighted)':'')+(rows?'; '+fmtInt(rows)+' matching row'+(rows===1?'':'s')+' in tables':'')+(gi<todo.length?'; searching the tables\u2026':'')+(total?' \u00b7 Enter: next':'');
  }
  show();
  (function step(){
    if(gen!==rsGen)return;
    var t0=now();
    while(gi<todo.length&&now()-t0<14){
      var g=todo[gi],end=Math.min(g.rows.length,ri+5000);
      for(;ri<end;ri++)if(g.rowText(ri).indexOf(ql)>=0)cnt++;
      if(ri>=g.rows.length){if(cnt)rowsBy[g.id]=cnt;gi++;ri=0;cnt=0;}
    }
    show();
    if(gi<todo.length)setTimeout(step,0);
  })();
}
function nextMark(){
  if(!rsMarks.length)return;
  if(rsCur>=0&&rsMarks[rsCur])rsMarks[rsCur].classList.remove('cur');
  rsCur=(rsCur+1)%rsMarks.length;var mk=rsMarks[rsCur];mk.classList.add('cur');
  var d=mk.closest('details');if(d)d.open=true;
  mk.scrollIntoView({block:'center'});
}
/* ------------------------------------------------------------------ help, toggles, print */
function openHelp(){helpReturn=D.activeElement;help.hidden=false;var b=help.querySelector('[data-act="help-close"]');if(b)b.focus();}
function closeHelp(){help.hidden=true;if(helpReturn&&helpReturn.focus)try{helpReturn.focus();}catch(e){}helpReturn=null;}
function updateToggles(){
  var t=$('act-theme'),d=$('act-density'),z=$('act-tz');
  if(t)t.textContent='Theme: '+(prefs.theme==='dark'?'Dark':prefs.theme==='light'?'Light':'System');
  if(d){d.textContent='Density: '+(prefs.density==='compact'?'Compact':'Comfortable');d.setAttribute('aria-pressed',prefs.density==='compact'?'true':'false');}
  if(z){var zt=tzText();z.textContent='Dates: '+(zt.length>34?zt.split(' \u00b7 ')[0]:zt);z.title='Dates shown in '+zt+'. Click to choose the time zone.';z.setAttribute('aria-haspopup','dialog');}
}
function repaintAll(){grids.forEach(function(g){if(g.ready){g.rh=rowH();g.measured=false;g.head();g.paint();}});if(drG)renderDrawer();}
var printOpened=[];
function fillStatic(g){
  if(!g.ready||!g.view)return;
  var tb=D.querySelector('#s-'+g.id+' tbody'),note=$('sn-'+g.id);if(!tb)return;
  var k=Math.min(g.m.print||0,g.view.length),cap=g.m.cell||2000,h=[],p,i,r,c,s;
  for(p=0;p<k;p++){
    i=g.view[p];r=g.rows[i];h.push('<tr><td class="num">'+fmtInt(i+g.first)+'</td>');
    for(c=0;c<g.nf;c++){s=c===g.tagc&&isObj(r[c])&&Array.isArray(r[c].j)?r[c].j.map(cellText).join('; '):cellText(r[c]);if(s.length>cap)s=s.slice(0,cap)+'\u2026 ('+fmtInt(s.length)+' characters)';h.push('<td>'+esc(s)+'</td>');}
    h.push('</tr>');
  }
  tb.innerHTML=h.join('');
  if(note)note.textContent='Printed: the first '+fmtInt(k)+' of the '+fmtInt(g.view.length)+' rows shown'+(g.view.length<g.rows.length||g.sort.length?' ('+g.describe()+'; '+fmtInt(g.rows.length)+' rows in the table)':'')+(k<g.view.length?'. More rows are not printed (limit html_print_rows); download the rows or use the interactive view.':'.');
}
/* ------------------------------------------------------------------ events */
function gridOf(el){var ge=el&&el.closest?el.closest('.grid'):null;return ge?gridById[ge.getAttribute('data-grid')]:null;}
function onClick(e){
  var t=e.target,el,g,a,c;
  if(t===help){closeHelp();return;}
  if(R.classList.contains('toc-open')&&!$('toc').contains(t)&&!(t.closest&&t.closest('[data-act="toc"]')))tocOpen(false);
  if(!pop.hidden&&!pop.contains(t)&&t!==popAnchor&&!(popAnchor&&popAnchor.contains(t)))closePop();
  el=t.closest?t.closest('[data-p]'):null;
  if(el&&pop.contains(el)){if(el.tagName==='BUTTON'){popAction(el.getAttribute('data-p'),el);}return;}
  el=t.closest?t.closest('[data-act]'):null;
  if(el){actClick(el.getAttribute('data-act'),el,e);return;}
  g=gridOf(t);
  if(g){
    lastGrid=g;
    el=t.closest('[data-g]');
    if(el){a=el.getAttribute('data-g');
      if(a==='cols')openCols(g,el);else if(a==='dl')openDl(g,el);else if(a==='clear')g.clearAll();
      return;}
    el=t.closest('.sortb');
    if(el){c=Number(el.getAttribute('data-c'));sortClick(g,c,e.shiftKey);return;}
    el=t.closest('.fmenu');
    if(el){c=Number(el.getAttribute('data-c'));if(popKind==='filter'&&popGrid===g&&popCol===c)closePop();else openFilter(g,c,el);return;}
    el=t.closest('tr[data-p]');
    if(el){var p=Number(el.getAttribute('data-p'));g.act=p;var td=t.closest('td');g.actCol=td?td.cellIndex-1:-1;
      if(W.matchMedia&&W.matchMedia('(pointer:coarse)').matches){openDrawer(g,p,true);return;}
      g.paint();if(drG===g&&!dr.hidden){drP=p;g.openRow=g.view[p];renderDrawer();scheduleHash(g);}}
    return;
  }
  el=t.closest?t.closest('table.st th .ss'):null;
  if(el){sortSimple(el);return;}
  el=t.closest?t.closest('#toc a[href^="#"]'):null;
  if(el){var id=decodeURIComponent(el.getAttribute('href').slice(1)),tg=$(id);if(tg){e.preventDefault();var d=tg.closest('details');if(d)d.open=true;tg.scrollIntoView();setCur(id);var rs=$('rs');
    if(rs&&rs.value.trim()&&gridById[id]&&gridById[id].ready){var gg=gridById[id];gg.qEl.value=rs.value.trim();gg.q=gg.qEl.value;gg.qh=gg.q.toLowerCase();gg.refilter();}
    try{tg.focus({preventScroll:true});}catch(x){}if(narrow())tocOpen(false);}}
}
function sortClick(g,c,add){
  var k=g.sortPos(c);
  if(add){if(k<0)g.sort.push([c,1]);else if(g.sort[k][1]>0)g.sort[k][1]=-1;else g.sort.splice(k,1);}
  else{if(g.sort.length===1&&k===0){if(g.sort[0][1]>0)g.sort[0][1]=-1;else g.sort=[];}else g.sort=[[c,1]];}
  g.head();g.resort();scheduleHash(g);
  var b=g.thead.querySelector('.sortb[data-c="'+c+'"]');if(b)b.focus();
}
function sortSimple(btn){
  var th=btn.closest('th'),table=th.closest('table'),tb=table.tBodies[0];if(!tb)return;
  var idx=th.cellIndex,dir=th.getAttribute('aria-sort')==='ascending'?-1:1,rows=[].slice.call(tb.rows);
  [].forEach.call(th.parentNode.cells,function(x){x.setAttribute('aria-sort','none');});
  th.setAttribute('aria-sort',dir>0?'ascending':'descending');
  var key=function(r){var td=r.cells[idx];if(!td)return '';var v=td.getAttribute('data-v');return v!==null?v:td.textContent.trim();};
  var allNum=rows.every(function(r){var k=key(r).replace(/,/g,'');return k===''||NUM_RE.test(k);});
  rows.sort(function(a,b){var x=key(a),y=key(b);if(allNum){x=parseFloat(x.replace(/,/g,''))||0;y=parseFloat(y.replace(/,/g,''))||0;}else{x=x.toLowerCase();y=y.toLowerCase();}return x<y?-dir:(x>y?dir:0);});
  rows.forEach(function(r){tb.appendChild(r);});
}
function tocOpen(open){
  var b=$('act-toc');R.classList.toggle('toc-open',open);if(b)b.setAttribute('aria-expanded',open?'true':'false');
  if(open){var f=$('rs')||D.querySelector('#toc a');if(f)try{f.focus();}catch(x){}}
  else if(b&&D.activeElement&&$('toc').contains(D.activeElement))try{b.focus();}catch(x){}
}
function narrow(){return W.innerWidth<1024;}
function actClick(a,el,e){
  if(a==='toc'){tocOpen(!R.classList.contains('toc-open'));return;}
  if(a==='theme'){prefs.theme=prefs.theme===''?'light':prefs.theme==='light'?'dark':'';applyPrefs();savePrefs();updateToggles();}
  else if(a==='density'){prefs.density=prefs.density==='compact'?'':'compact';applyPrefs();savePrefs();updateToggles();repaintAll();}
  else if(a==='tz'){openTz(el);return;}
  else if(a==='print')W.print();
  else if(a==='help')openHelp();
  else if(a==='help-close')closeHelp();
  else if(a==='d-prev')moveDrawer(-1);
  else if(a==='d-next')moveDrawer(1);
  else if(a==='d-close')closeDrawer();
  else if(a==='d-json'&&drG)copyText(rowJson(drG,drG.rows[drG.view[drP]]),'The row (JSON)');
  else if(a==='d-tsv'&&drG)copyText(rowTsv(drG,drG.rows[drG.view[drP]]),'The row (TSV)');
  else if(a==='d-blob'&&drG){openBlob(drG,Number(el.getAttribute('data-c')),drG.rows[drG.view[drP]]);}
  else if(a.indexOf('bi-')===0){blobAction(a,el,e);}
  else if(a==='d-copy'&&drG){var c=Number(el.getAttribute('data-c'));copyText(csvCell(drG.rows[drG.view[drP]][c]),'The value of '+drG.m.fields[c]);}
  else if(a==='totop'){W.scrollTo(0,0);var mn=$('main');if(mn)try{mn.focus({preventScroll:true});}catch(x){}}
  else if(a==='copy-code'){var blk=el.closest('.codeblock'),pre=blk&&blk.querySelector('pre');if(pre)copyText(pre.textContent,'The SQL');}
  else if(a==='copy-all'){var sel=el.getAttribute('data-sel')||'pre.sql',txt=[].map.call(D.querySelectorAll(sel),function(p){return p.textContent;}).join('\n\n');copyText(txt,'All SQL');}
}
function onKey(e){
  var t=e.target,tag=t&&t.tagName,typing=tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||(t&&t.isContentEditable);
  if(e.key==='Escape'){
    if(!pop.hidden){closePop();e.preventDefault();return;}
    if(bi&&!bi.hidden){closeBlob();e.preventDefault();return;}
    if(!help.hidden){closeHelp();e.preventDefault();return;}
    if(!dr.hidden){closeDrawer();e.preventDefault();return;}
    if(R.classList.contains('toc-open')){tocOpen(false);e.preventDefault();return;}
    if(typing&&t.id==='rs'&&t.value){t.value='';reportSearch('');return;}
    if(typing&&t.blur)t.blur();
    return;
  }
  if(t&&t.id==='rs'&&e.key==='Enter'){e.preventDefault();nextMark();return;}
  if(typing&&t.classList.contains('fi')&&e.key==='Enter'){var g0=gridOf(t);if(g0)g0.setExpr(Number(t.getAttribute('data-c')),t.value);return;}
  if(typing||e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==='?'){e.preventDefault();if(help.hidden)openHelp();else closeHelp();return;}
  var g=activeGrid();
  if(e.key==='/'){e.preventDefault();if(g)g.focusSearch();else{var rs=$('rs');if(rs)rs.focus();}return;}
  if(!g||!g.view)return;
  var inGrid=t===g.wrap||(dr&&dr.contains(t));
  if(e.key==='j'||(inGrid&&e.key==='ArrowDown')){e.preventDefault();g.move(1);return;}
  if(e.key==='k'||(inGrid&&e.key==='ArrowUp')){e.preventDefault();g.move(-1);return;}
  if(inGrid&&e.key==='PageDown'){e.preventDefault();g.move(g.visibleRows());return;}
  if(inGrid&&e.key==='PageUp'){e.preventDefault();g.move(-g.visibleRows());return;}
  if(inGrid&&e.key==='Home'){e.preventDefault();g.move(-g.view.length);return;}
  if(inGrid&&e.key==='End'){e.preventDefault();g.move(g.view.length);return;}
  if(e.key==='Enter'&&(t===g.wrap||t===D.body)&&g.act>=0){e.preventDefault();openDrawer(g,g.act,true);return;}
  if(e.key==='c'&&g.act>=0){
    var r=g.rows[g.view[g.act]],cols=g.vis(),c=g.actCol>=0&&g.actCol<cols.length?cols[g.actCol]:-1;
    if(c>=0)copyText(csvCell(r[c]),'The value of '+g.m.fields[c]);else copyText(rowTsv(g,r),'The row (TSV)');
  }
}
function onInput(e){
  var t=e.target;
  if(t.classList&&t.classList.contains('fi')){var g=gridOf(t);if(g){lastGrid=g;g.fdeb(Number(t.getAttribute('data-c')),t.value);scheduleHash(g);}}
  else if(t.id==='rs')rsDeb(t.value);
}
var rsDeb=debounce(function(v){reportSearch(v);},250);
function onChange(e){
  var t=e.target;
  if(pop&&pop.contains(t)&&t.getAttribute('data-p')){popAction(t.getAttribute('data-p'),t);return;}
  var g=gridOf(t);if(g&&(t.classList.contains('gq')||t.classList.contains('gmo')))scheduleHash(g);
}
function onPointerDown(e){
  var t=e.target;if(!t.classList||!t.classList.contains('rz'))return;
  var g=gridOf(t);if(!g)return;
  e.preventDefault();
  var c=Number(t.getAttribute('data-c')),x0=e.clientX,w0=g.w[c],k=g.vis().indexOf(c),col=g.cg.children[k+1];
  function mv(ev){var w=Math.max(48,Math.min(1200,w0+ev.clientX-x0));g.w[c]=w;if(col)col.style.width=w+'px';g.table.style.width=g.tableWidth()+'px';}
  function up(){D.removeEventListener('pointermove',mv);D.removeEventListener('pointerup',up);}
  D.addEventListener('pointermove',mv);D.addEventListener('pointerup',up);
}
/* ------------------------------------------------------------------ start */
function loadAll(){
  var datas=[].slice.call(D.querySelectorAll('script.gdata')),di=0;
  (function step(){
    var t0=now();
    while(di<datas.length&&now()-t0<14){
      var el=datas[di++],g=gridById[el.getAttribute('data-grid')];
      if(g&&!g.ready){
        try{var rows=JSON.parse(el.textContent);for(var k=0;k<rows.length;k++)g.rows.push(rows[k]);}
        catch(x){g.err='Some rows could not be read from this file: '+(x&&x.message||x);}
        g.loaded++;
        if(g.loaded>=g.m.chunks)g.finish();else g.progress();
      }
      if(el.parentNode)el.parentNode.removeChild(el);
    }
    if(di<datas.length){setTimeout(step,0);return;}
    grids.forEach(function(g){g.finish();});
    live=true;
    var rs=$('rs');if(rs&&rs.value)reportSearch(rs.value);
  })();
}
function init(){
  bi=$('blobi');pop=$('pop');dr=$('drawer');drBody=$('drawer-body');drTitle=$('drawer-title');drPos=$('drawer-pos');help=$('help');toast=$('toast');toTop=$('totop');
  updateToggles();
  var tocT=$('toc-toggle'),tocS=$('toc-show'),layout=$('layout');
  function setToc(hidden){
    if(!layout)return;
    layout.classList.toggle('toc-hidden',hidden);
    if(tocT)tocT.setAttribute('aria-expanded',hidden?'false':'true');
    try{W.localStorage.setItem('sga-toc-hidden',hidden?'1':'0');}catch(e){}
  }
  if(layout){
    try{if(W.localStorage.getItem('sga-toc-hidden')==='1')setToc(true);}catch(e){}
    if(tocT)tocT.addEventListener('click',function(){setToc(true);});
    if(tocS)tocS.addEventListener('click',function(){setToc(false);});
  }
  [].forEach.call(D.querySelectorAll('script.gmeta'),function(el){
    try{var m=JSON.parse(el.textContent);if(!$('g-'+m.id))return;var g=new Grid(m);grids.push(g);gridById[m.id]=g;}catch(x){}
  });
  var st=stDec(W.location.hash);curSec=st.sec||'';
  if(st.tbl&&gridById[st.tbl])gridById[st.tbl].pending=st;
  D.addEventListener('click',onClick);
  D.addEventListener('dblclick',function(e){
    var g=gridOf(e.target),rz=e.target.closest&&e.target.closest('.rz[data-c]'),tr=e.target.closest&&e.target.closest('tr[data-p]');
    if(g&&rz){e.preventDefault();g.fit(Number(rz.getAttribute('data-c')));return;}
    if(g&&tr)openDrawer(g,Number(tr.getAttribute('data-p')),true);});
  D.addEventListener('keydown',onKey);
  D.addEventListener('input',onInput);
  D.addEventListener('change',onChange);
  D.addEventListener('pointerdown',onPointerDown);
  D.addEventListener('focusin',function(e){var g=gridOf(e.target);if(g)lastGrid=g;});
  W.addEventListener('resize',debounce(function(){grids.forEach(function(g){g.paint();});closePop();},120));
  var tt=0;W.addEventListener('scroll',function(){if(tt)return;tt=W.requestAnimationFrame(function(){tt=0;if(toTop)toTop.hidden=W.pageYOffset<600;});},{passive:true});
  W.addEventListener('hashchange',function(){
    var s=stDec(W.location.hash);
    if(s.tbl&&gridById[s.tbl]){var g=gridById[s.tbl];if(g.ready){g.applyState(s);g.head();g.refilter();}else g.pending=s;}
    var id=s.tbl||s.sec,el=id&&$(id);if(el){var d=el.closest('details');if(d)d.open=true;if(W.location.hash.indexOf('=')>=0)el.scrollIntoView();curSec='';setCur(id);}
  });
  W.addEventListener('beforeprint',function(){printOpened=[];[].forEach.call(D.querySelectorAll('details:not([open])'),function(d){d.open=true;printOpened.push(d);});grids.forEach(fillStatic);});
  W.addEventListener('afterprint',function(){printOpened.forEach(function(d){d.open=false;});printOpened=[];});
  setupSpy();
  var tg=st.tbl||st.sec;
  if(tg&&W.location.hash.indexOf('=')>=0){var el=$(tg);if(el){var d=el.closest('details');if(d)d.open=true;setTimeout(function(){el.scrollIntoView();},0);}}
  loadAll();
}
if(D.readyState==='loading')D.addEventListener('DOMContentLoaded',init);else init();
})();
"""

# the named time zones of the Dates button (the same names as the app's zone lists)
ZONES_JS = "var ZONES=%s;" % json.dumps(dict((str(k), v) for k, v in sorted(ZONE_NAMES.items())),
                                       ensure_ascii=True, sort_keys=True)

JS = MAIN_JS.replace("/*PURE*/", ZONES_JS + VALUES_JS + FILTER_JS + STATE_JS)
