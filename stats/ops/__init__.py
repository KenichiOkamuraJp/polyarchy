"""stats の運用（ops）：freshness＝取得元の更新の存否確認・（将来）refresh＝差分更新・usage＝利用集計。

設計＝docs/運用設計.md §2・§4。ingest（取込そのもの）と eval（品質ゲート）のどちらでもない
「サービス提供時の運用」の置き場。読み取り専用のもの（freshness・usage）はいつ実行してもよい。
"""
