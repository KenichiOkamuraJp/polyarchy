"""HFモデルを HF_HOME へ事前DLして EBS に固定する（初回サービス起動の外部依存を消す）。

対象＝本番の格納/検索埋め込みとリランカー（recommendations/core/config.py・recommendations/core/embeddings.py・recommendations/core/rerankers.py）：
  - cl-nagoya/ruri-v3-310m                                     … PRODUCTION_EMBEDDING(ruri_v3_310m_pfx)
  - hotchpotch/japanese-reranker-cross-encoder-xsmall-v1       … PRODUCTION_RERANKER(jp_reranker_xsmall_v1)

snapshot_download でリポジトリ丸ごと HF_HOME/hub に落とす。実行前に HF_HOME を EBS 上の
固定パス（例 /opt/polyarchy/models）に設定しておくこと。ログは日本語。
"""
import os
import sys

# 本番で実際に使う2モデル（config が指すもの）。増やす時はここに足す。
REPOS = [
    "cl-nagoya/ruri-v3-310m",
    "hotchpotch/japanese-reranker-cross-encoder-xsmall-v1",
]


def main() -> int:
    hf_home = os.environ.get("HF_HOME", "(未設定)")
    print(f"[prefetch] HF_HOME={hf_home} へモデルを事前DLします", file=sys.stderr)
    try:
        from huggingface_hub import snapshot_download
    except Exception as e:  # sentence-transformers/transformers が入っていれば同梱される
        print(f"[prefetch] huggingface_hub が読めません: {e}", file=sys.stderr)
        return 1

    for repo in REPOS:
        print(f"[prefetch] 取得開始: {repo}", file=sys.stderr)
        # 大きめのバイナリを含むため tqdm は静かに。失敗は即エラーで気づけるように。
        snapshot_download(repo_id=repo, tqdm_class=None)
        print(f"[prefetch] 取得完了: {repo}", file=sys.stderr)

    print("[prefetch] 全モデルの事前DL完了", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
