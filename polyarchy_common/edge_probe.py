"""
外からの到達の確認（運用設計 §1.1・2026-10-12）。

箱の中の監視（localhost の /healthz・ログ）は、箱は健全なのに利用者がつなげない種類の異常（トンネル・DNS・エッジの規則・
ディスカバリ・IdP）を見られない。箱自身が**公開 URL** を、利用者のクライアントと同じ入口から、資格情報なしで叩いて確かめる。

サービスごと（箱の `/etc/polyarchy/<サービス>.env` に案 B の公開 URL があるもの＝一覧を持たない）：
  healthz … `https://<ホスト>/healthz` が 200
  mcp     … 守るパスへのトークン無しの要求が 401 で、`WWW-Authenticate` がディスカバリの場所を指す
            （403＝エッジの規則が止めている／200＝認証なしで通っている）
  prm     … `/.well-known/oauth-protected-resource` が 200 で、`resource` と発行者が env と一致
            （一致しない＝そのホストが別のサービスに届いている・発行者の設定の食い違い）
  aud     … env の aud が公開 URL（resource）と同じ文字列（三点一致のうち箱の側の 2 点。IdP の側の
            Resource indicator はトークンが無いと確かめられない＝ここでは見ない）
IdP（発行者ごと）：ディスカバリ（OIDC → RFC 8414 の順）が取れ、`jwks_uri` から公開鍵が 1 つ以上取れる。

出力＝1 行 1 結果（`edge <サービス> <1|0>`・`idp <1|0>`。deploy/bootstrap/edge_probe.sh がメトリクスにする）と状態ファイル
（運用ダッシュボード①が読む）。★パスは出さない（秘密パスの構成では合言葉）＝失敗は確認の名前と HTTP の状態だけで表す。
同じ視点の限界＝箱ごと落ちれば確認も止まる（そちらは health の missing が拾う）。

実行：python -m polyarchy_common.edge_probe            （箱＝/etc/polyarchy を読む）
      python -m polyarchy_common.edge_probe --resource-url https://<ホスト>/mcp --issuer https://<発行者>   （手元から 1 件）
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from polyarchy_common.access import idp_config

ROOT = Path(__file__).resolve().parents[1]
ENV_DIR = Path("/etc/polyarchy")
STATE_PATH = ROOT / "ops" / "dashboard" / "edge_probe.json"   # ops/dashboard/ は git 外・箱で生まれる
TIMEOUT = 10
USER_AGENT = "polyarchy-edge-probe/1"
# 守るパスへの探りは、ガードの test（deploy/scripts/cloudflare-guard.sh の probe）と同じ形の要求にする。
PROBE_BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()


def http_fetch(url: str, method: str = "GET") -> tuple[int, dict, bytes]:
    """(status, 小文字のヘッダ, 本文)。届かない（DNS・接続・時間切れ）は status 0。4xx・5xx も例外にしない。"""
    headers = {"user-agent": USER_AGENT, "accept": "application/json, text/event-stream"}
    data = None
    if method == "POST":
        data, headers["content-type"] = PROBE_BODY, "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(65536)
    except Exception:  # noqa: BLE001
        return 0, {}, b""


def _st(status: int) -> str:
    return "届かない" if status == 0 else str(status)


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def check_service(target: dict, fetch=http_fetch) -> list[str]:
    """失敗した確認の名前の一覧（空＝全部通った）。`target`＝{service, resource_url, issuer, aud}。"""
    res = target["resource_url"]
    parts = urlsplit(res)
    base = f"{parts.scheme}://{parts.netloc}"
    prm_url = f"{base}/.well-known/oauth-protected-resource"
    failed = []

    st, _, _ = fetch(f"{base}/healthz")
    if st != 200:
        failed.append(f"healthz({_st(st)})")

    st, hdr, _ = fetch(res, "POST")
    if st == 200:
        failed.append("mcp(200＝認証なしで通る)")
    elif st != 401:
        failed.append(f"mcp({_st(st)})")
    elif f'resource_metadata="{prm_url}"' not in hdr.get("www-authenticate", ""):
        failed.append("mcp(WWW-Authenticate)")

    st, _, body = fetch(prm_url)
    if st != 200:
        failed.append(f"prm({_st(st)})")
    else:
        meta = _json(body)
        if not isinstance(meta, dict):
            failed.append("prm(本文)")
        elif meta.get("resource") != res:
            failed.append("prm(resource)")
        elif target["issuer"] not in (meta.get("authorization_servers") or []):
            failed.append("prm(issuer)")

    if target.get("aud") and target["aud"] != res:
        failed.append("aud≠resource")
    return failed


def check_idp(issuer: str, fetch=http_fetch) -> str:
    """失敗の理由（空＝通った）。認証の側（access.make_oidc_verifier）と同じ順でディスカバリを引く。"""
    st, meta = 0, None
    for well_known in ("openid-configuration", "oauth-authorization-server"):
        st, _, body = fetch(f"{issuer.rstrip('/')}/.well-known/{well_known}")
        meta = _json(body) if st == 200 else None
        if isinstance(meta, dict) and meta.get("jwks_uri"):
            break
        meta = None
    if meta is None:
        return f"discovery({_st(st)})"
    st, _, body = fetch(meta["jwks_uri"])
    if st != 200:
        return f"jwks({_st(st)})"
    keys = _json(body)
    if not isinstance(keys, dict) or not keys.get("keys"):
        return "jwks(鍵が無い)"
    return ""


def load_targets(env_dir: Path = ENV_DIR) -> list[dict]:
    """サービスごとの env（bootstrap ⑧ の write_service_env が書く）から確認の対象を読む。サービス名＝ファイル名
    （health のメトリクスの dimension と同じ）。env の名前は `access.idp_config` だけが知っている＝公開 URL（resource）の
    無い env（認証の無い構成・deploy.env 等）は対象外。"""
    out = []
    for f in sorted(Path(env_dir).glob("*.env")):
        try:
            kv = dict(line.split("=", 1) for line in f.read_text(encoding="utf-8").splitlines() if "=" in line and not line.startswith("#"))
        except Exception:  # noqa: BLE001
            continue
        cfg = idp_config({k.strip(): v.strip() for k, v in kv.items()})
        if cfg and cfg["resource_url"]:
            out.append({"service": f.stem, **cfg})
    return out


def probe(targets: list[dict], fetch=http_fetch, out=None, state_path: Path | None = None, now: str | None = None) -> dict:
    """対象を確かめ、結果の行を `out` に書き、状態ファイルを置く。"""
    out = out or sys.stdout
    result: dict = {"ts": now or datetime.now(timezone.utc).isoformat(timespec="seconds"), "services": {}, "idp": None}
    for t in targets:
        failed = check_service(t, fetch)
        result["services"][t["service"]] = {"host": urlsplit(t["resource_url"]).netloc, "ok": not failed, "failed": failed}
        print(f"edge {t['service']} {0 if failed else 1}", file=out)
    issuers = sorted({t["issuer"] for t in targets})
    if issuers:
        reasons = [r for r in (check_idp(i, fetch) for i in issuers) if r]
        result["idp"] = {"ok": not reasons, "failed": "・".join(reasons)}
        print(f"idp {0 if reasons else 1}", file=out)
    if state_path is not None and targets:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            tmp.replace(state_path)
        except Exception as e:  # noqa: BLE001  状態ファイルは見せるためだけ＝書けなくても確認の結果は送る
            print(f"[edge-probe] 状態ファイルを書けない: {e!r}", file=sys.stderr)
    return result


def run(env_dir: Path = ENV_DIR, state_path: Path = STATE_PATH, fetch=http_fetch, out=None, now: str | None = None) -> int:
    probe(load_targets(env_dir), fetch, out, state_path, now)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="外からの到達の確認（公開 URL を資格情報なしで叩く）")
    ap.add_argument("--env-dir", type=Path, default=ENV_DIR)
    ap.add_argument("--resource-url", help="手元から 1 件だけ確かめる（env を読まない・状態ファイルを書かない）")
    ap.add_argument("--issuer")
    ap.add_argument("--aud", default="")
    a = ap.parse_args(argv)
    if a.resource_url:
        if not a.issuer:
            ap.error("--resource-url には --issuer が要る")
        r = probe([{"service": "manual", "resource_url": a.resource_url, "issuer": a.issuer, "aud": a.aud}])
        print(json.dumps({"failed": r["services"]["manual"]["failed"], "idp": r["idp"]}, ensure_ascii=False), file=sys.stderr)
        return 0
    return run(a.env_dir)


if __name__ == "__main__":
    raise SystemExit(main())
