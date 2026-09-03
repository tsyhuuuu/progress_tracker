# progress_tracker

3台のマシン（MacOS / Desktop1・Win+Docker / Desktop・Ubuntu）に分散して走らせている
`embodied_perceptron`の`train_auto.sh`の実験群を、(1) 何のためにやっているかの目的メモと、
(2) 条件×seed単位の進捗、の2軸で見失わないための最小限のツール一式。

**独立したgitリポジトリ**（`embodied_perceptron/supervision/`から切り出したもの）。
`embodied_perceptron`の中には住んでいないので、各マシンの`embodied_perceptron`チェックアウトが
どこにあるかを`config.yaml`で教えてやる必要がある（後述）。

## 中身

| ファイル | 役割 | 更新方法 |
|---|---|---|
| `config.yaml` | このマシンの`embodied_perceptron`チェックアウトへのパス（`repo_root`） | 手動で作成（`config.yaml.example`をコピー）。**gitignore対象**、マシンごとに内容が違う |
| `registry.yaml` | 実験ごとの「目的」台帳。`train_auto.sh`の`configs[]`の1行に対応する1エントリ | 手動で編集（新しい実験を始めたら1エントリ追加） |
| `scan_status.py` | `config.yaml`の`repo_root`配下のローカル`results/`を読んで進捗JSONを生成 | `uv run python scan_status.py` を実行するたび自動更新 |
| `status/<machine>.json` | `scan_status.py`の出力（マシン名はホスト名から自動決定、`--machine`で上書き可） | `scan_status.py`が書く。手で編集しない |
| `build_dashboard.py` | `registry.yaml` + `status/*.json` を束ねてプログレスバー付きの1枚HTMLを生成 | `uv run python build_dashboard.py` |
| `dashboard.html` | `build_dashboard.py`の出力（Artifactとして公開してスマホから見る用） | 生成物。手で編集しない |

## 初回セットアップ（マシンごとに1回）

```bash
# 1. 依存関係（pyyaml/pandas）を用意
uv sync

# 2. このマシンのembodied_perceptronチェックアウトへのパスを設定
cp config.yaml.example config.yaml
# config.yamlのrepo_rootを編集。デフォルトは「progress_tracker と embodied_perceptron が
# 兄弟ディレクトリ」前提の ../embodied_perceptron。レイアウトが違うマシンでは絶対パスに書き換える。
```

## 使い方（今のところ手動）

```bash
# 1. このマシンの進捗をスキャン（読み取り専用、副作用なし。どのマシンでも・いつでも実行可）
uv run python scan_status.py --report

# 2. ダッシュボードHTMLを再生成
uv run python build_dashboard.py

# 3. Claude Codeに「ダッシュボード更新して」と頼むと dashboard.html を
#    Artifactとして再公開してくれる（スマホなどどこからでも同じURLで最新版が見られる）
```

`registry.yaml`は`train_auto.sh`の`configs[]`に新しい行を足す（or コメントアウトを解除する）
たびに、対応するエントリを1つ足す/statusを更新する運用を想定。

## まだやっていないこと（次のステップの候補）

1. **他2台のマシンへの導入**：それぞれで本READMEの「初回セットアップ」を実施
   （`uv sync`＋`config.yaml`作成）し、`uv run python scan_status.py`が動くことを確認。
2. **`status/*.json`のGitHub経由の同期**：各マシンの`train_auto.sh`ループに、各イテレーション後
   `scan_status.py`を実行して`status/<machine>.json`をcommit+pushする一手間を追加。
   3台ともこのリポジトリへ非対話でpushできる状態か確認してから。
3. **ダッシュボードの自動更新**：Mac側でgit pull→`build_dashboard.py`→Artifact再publishを
   定期実行（`/loop`等）するか、都度Claude Codeに頼むかの運用を決める。

## 設計メモ

- 進捗の判定は`embodied_perceptron`側`tools/resume_or_train.py`の`read_eval_progress()`と
  同じロジック（`results/<condition>/<timestamp>/plots/eval.csv`の最終行の`env_steps`を、
  条件yamlの`max_env_steps`と比較）。手動記録は一切不要——既存の結果ツリーを読むだけ。
- ラン単位のステータスは4種類：`active`（直近20分以内に更新）/ `stalled`（それより前に
  更新が止まっている＝クラッシュか、単にこのマシンの番が来ていないだけか、ファイルからは
  区別できない）/ `done`（`env_steps >= max_env_steps`）/ `no_eval_yet`（`eval.csv`がまだ無い）。
- `passive_joint_evaluation`のように結果がDVC管理（gdrive remote）で、かつそのマシンで
  `dvc pull`していない場合は、`scan_status.py`は「このマシンには無し」として扱う
  （クラッシュとは区別される）。
- `repo_root`の解決優先順位：`--repo-root`引数 > `config.yaml`の`repo_root` > 明確なエラーで
  停止（`config.yaml`が無い/`repo_root`が指す先に`automation/`が無い、のいずれか）。
  `status/<machine>.json`にも実際に使われた`repo_root`を記録し、どこを見た結果かを追跡できる
  ようにしている。
