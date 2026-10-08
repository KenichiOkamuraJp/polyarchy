"""HFモデルを HF_HOME へ事前DLして EBS に固定する（初回サービス起動の外部依存を消す）。

対象＝本番の埋め込みとリランカー＝polyarchy_retrieval.models.PINNED_REVISIONS（リポジトリ → commit）の全部。
読み込み側（同じ models.py）と同じ commit を取る＝箱が重みを取り直すたびに上流の main を解決し直さない（2026-10-09）。
一覧を写さない（以前はここに 2 つのリポジトリ名を手で写していた）。bootstrap ③ で本パッケージを pip -e 済みなので import できる。

snapshot_download でリポジトリ丸ごと HF_HOME/hub に落とす。実行前に HF_HOME を EBS 上の
固定パス（例 /opt/polyarchy/models）に設定しておくこと。ログは日本語。
"""
import os
import sys


def main() -> int:
    hf_home = os.environ.get("HF_HOME", "(未設定)")
    print(f"[prefetch] HF_HOME={hf_home} へモデルを事前DLします", file=sys.stderr)
    try:
        from huggingface_hub import snapshot_download

        from polyarchy_retrieval.models import PINNED_REVISIONS
    except Exception as e:  # sentence-transformers/transformers が入っていれば同梱される
        print(f"[prefetch] huggingface_hub／polyarchy_retrieval が読めません: {e}", file=sys.stderr)
        return 1

    for repo, rev in PINNED_REVISIONS.items():
        print(f"[prefetch] 取得開始: {repo}@{rev[:12]}", file=sys.stderr)
        # 大きめのバイナリを含むため tqdm は静かに。失敗は即エラーで気づけるように。
        snapshot_download(repo_id=repo, revision=rev, tqdm_class=None)
        print(f"[prefetch] 取得完了: {repo}@{rev[:12]}", file=sys.stderr)

    print("[prefetch] 全モデルの事前DL完了", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
