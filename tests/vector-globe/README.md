# Vector Globe

HTML + JavaScript + WebGL2 で動く、GSHHG 2.3.7 ベースのベクター地球儀です。

## Status

**Complete for the 1000% display goal.**

日本で4K・1000%表示を確認した品質と同じ full LOD4 を全球へ展開しました。

- 100%〜500%: 全球の軽量 delivery bundle
- 1000%: 全球の full LOD4
- full LOD4: 451 tiles / 154.487 MiB
- delivery LOD0–3: 226 files / 30.957 MiB
- 公開データ合計: 185.444 MiB
- 最大単一タイル: 約3.53 MiB
- トップページからはリンクしない独立テスト公開

公開先:

https://2rwa.github.io/2rwa-public/tests/vector-globe/

## Controls

- ドラッグ: 地球を回転
- ホイール / 2本指: ズーム
- Zoom slider: 100%〜1000%
- 日本: 日本付近へ移動
- 全球: 全球表示へ戻る

## Data

Source: GSHHG / GSHHS 2.3.7  
Upstream: GenericMappingTools/gshhg-gmt  
License: GNU LGPL v3

LOD0–LOD3 は通信リクエスト数を減らすため bundle 配信し、LOD4 は10°×10°の canonical full-resolution tiles を必要領域だけ読み込みます。

## Completion criterion

このプロジェクトの目的は「ブラウザ上で1000%程度まで拡大しても十分細かい全球ベクター地球儀」です。

2026-09-24、以下を満たしたためこの目的については完成扱いとします。

- 日本で4K / 1000%表示の品質を実機確認
- full LOD4 を全球へ展開
- 日本、ノルウェー、チリ南部、日付変更線付近、南極をブラウザQA対象に設定
- スマホ向けGPU cache制限とLOD切替を実装
- 低倍率はdelivery bundle、高倍率はcanonical full tileを使用
