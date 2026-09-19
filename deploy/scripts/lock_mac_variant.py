"""ロック（箱向け Linux x86_64・torch +cpu）から Mac 検証用の変種を派生する（RUNBOOK §7 ②）。

箱のロックは torch を CPU 版（`2.x+cpu`・download.pytorch.org）で固定しており、Mac（arm64）にはその版が無い。
そこで torch の行だけを PyPI の同じ版（例 2.13.0）に置き換え、PyPI が公開する全配布物の sha256 を付ける＝
Mac でも `--require-hashes` の規律を落とさずに同じ版でゲートを回せる。他のピンとハッシュは箱のロックと同一。
    python deploy/scripts/lock_mac_variant.py [ロック] [出力]   # 既定＝lock-recommendations-stats.txt → /tmp/lock-mac.txt
"""
import json
import pathlib
import re
import sys
import urllib.request

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "deploy/requirements/lock-recommendations-stats.txt")
dst = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/lock-mac.txt")
t = src.read_text(encoding="utf-8")
m = re.search(r"^torch==([0-9][^+\s\\]*)\+cpu", t, flags=re.M)
if not m:
    sys.exit("torch の +cpu ピンが見つからない（このロックは Mac 変種の対象外）")
ver = m.group(1)
t = re.sub(r"^--extra-index-url [^\n]*\n", "", t, flags=re.M)  # Mac は PyPI のみ
t = re.sub(r"^torch==[^\n]*\n(?:    --hash[^\n]*\n)*(?:    # via[^\n]*\n(?:    #   [^\n]*\n)*)?", "", t, flags=re.M)
j = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/torch/{ver}/json"))
hs = sorted({f["digests"]["sha256"] for f in j["urls"]})
t += f"torch=={ver} \\\n" + " \\\n".join(f"    --hash=sha256:{h}" for h in hs) + "\n    # via Mac 検証用（PyPI の同版・箱のロックは +cpu）\n"
dst.write_text(t, encoding="utf-8")
print(f"[lock-mac] {dst}（torch=={ver}・PyPI 配布物 {len(hs)} 件のハッシュ）")
