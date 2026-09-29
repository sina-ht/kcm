# WORKFLOW.md — カーネル設定を、生きたバージョン管理のワークブックとして運用する

`kcm` は、カーネル設定を「**バージョン管理されたテキストファイル**の小さなセット」と、破棄可能な Excel 表示の組み合わせとして扱うことを可能にします。テキストファイルこそが `git` で管理する**唯一の正 (source of truth)** であり、`.xlsx` は設定を見たり注釈をつけたりするために開く、再生成可能な表示です。

## レイアウト: 設定リポジトリはカーネルツリーと分離する

このワークフローは**2つの独立した対象**を扱います。両者は**同じリポジトリである必要はありません** — 標準的なレイアウトでは実際には別物です:

|                | 場所                                                             | 保持するもの                                                                 | バージョン管理 |
|----------------|-------------------------------------------------------------------|-----------------------------------------------------------------------|--------------------|
| **設定リポジトリ** | 自前の `git` リポジトリ — *プロジェクトルート*、つまり `.kcmrc` が置かれる場所 | `config` (値)、`memos.csv` (注釈)、`history.csv` (監査証跡)、任意のローカル `kernel.xlsx` | **はい** — これがコミット対象 |
| **カーネルソースツリー** | `srcdir` が指すどこでも: チェックアウト、自前のリポジトリ、またはただのディレクトリ | kcm がメタデータ (型, タイトル, `depends`, デフォルト) を読む `Kconfig` ファイル | いいえ — kcm は**読むだけ** |

kcm は `srcdir` を単にファイルパスとして解決します (`srctree` を設定して `Kconfig` を解析するだけ)。ツリーに対して `git` を実行することはありません。すべての `git` 操作 — `HEAD:<config>` のベース、コミット、`--patch` のリファレンス — は `.kcmrc` が選択するプロジェクトルートである**設定リポジトリ**で行われます。したがって、デフォルトではツリーと設定は別々に置かれます:

```
workspace/
  linux/            <- カーネルソースツリー (自前のリポジトリ、またはただのチェックアウト)
  kernel-config/    <- 設定リポジトリ (プロジェクトルート。ここまたはそのサブディレクトリから kcm を実行)
     .kcmrc
     config          <- バージョン管理される設定
     memos.csv
     history.csv
     kernel.xlsx     <- 任意のローカル表示 (--commit-xlsx を渡さない限りコミットされない)
```

これが追加する1つの機械的なステップ: `make menuconfig` / `make olddefconfig` は結果を**カーネルツリー**内に書き出すため、ビルドの後は変更を記録する前にツリーの `.config` を設定リポジトリへコピーします。ツリーの `.config` は作業領域 (スクラッチ) であり、設定リポジトリの `config` が唯一の正です。

## 唯一の正 (source of truth) は何か

| ソース         | ファイル (設定リポジトリ内)    | 保持するもの                          |
|----------------|------------------------------|--------------------------------|
| 設定     | `config`                    | 実際の値              |
| 注釈    | `memos.csv` (`name,note`)    | 各オプションの「なぜ」      |
| 変更履歴 | `history.csv` (`batch,date,base_commit,new_commit,sheet,name,diff,note`) | 監査証跡 |

`.xlsx` はこれら3つ (メタデータ用の Kconfig ツリーも合わせて) の**投影**です。固有のデータを一切持ちません。破棄してもいつでも `dump` で再構築できます。したがって目安となる原則は — 3つのテキストファイルを `git` 上で完全かつ最新に保ち、設定を見たり編集したりしたくなったときにワークブックを再生成すること。すべての変更の後はテキストファイルへ永続化します (それがコミットに値する状態)。ワークブックは「今見ているもの」にすぎません。

## 約束事

- **`.kcmrc` は設定リポジトリに置き**、パスを保持します:

  ```ini
  [kcm]
  config  = config
  srcdir  = ../linux
  memo    = memos.csv
  history = history.csv
  xlsx    = kernel.xlsx
  ```

  用意すれば、以下のコマンドはフラグ不要になります — `kcm commit --note
  "..."` が記録＋コミットの全ステップです。カレントディレクトリから上層へたどって発見されるため、設定リポジトリ内のどこからでも `kcm` を実行できます (README を参照: [README](README.md#project-file-kcmrc))。
- **プロダクト/アーキテクチャごとに1セットのファイル**: `config`、`memos.csv`、`history.csv`、
  再生成可能な `kernel.xlsx`、すべて設定リポジトリ内。
- **作業シート** — `config` (デフォルトのシート)。**ベースラインシート (任意)** — 比較用に `baseline`。
- **列には固定した役割**: `memo` = あなたの注釈、`diff` = 直近の `diff-merge` による変更、`History` シート = 監査証跡 (`history.csv` の表示)。

---

## ステージ1 — ベースラインを確立する

カーネルツリー (チェックアウト、または自前のリポジトリ) と、カーネルが現在ビルドに使っている設定 (ベンダーの defconfig、既存の `config`、実行中システムの `/proc/config.gz` など) があります。その設定を設定リポジトリに置き、ワークブックをビルドして眺めましょう:

```sh
mkdir kernel-config && cd kernel-config && git init
printf '[kcm]\nconfig = config\nsrcdir = ../linux\nmemo = memos.csv\nhistory = history.csv\nxlsx = kernel.xlsx\n' > .kcmrc
cp ../linux/.config config            # または defconfig / /proc/config.gz をここにコピー

python3 kcm.py dump -o kernel.xlsx     # .kcmrc を使用 (config=config, srcdir=../linux)
git add .kcmrc config && git commit -m "baseline config"
```

`kernel.xlsx` には `config` シートが作られます: シンボルごとに1行 (フルな x86 設定なら約5000行)、`config` の順序で、型、タイトル、値、依存、デフォルト、そして空の `memo` 列が並びます。

Excel で開いて、全体像を把握しましょう:

- フィルター/並び替え/検索で、実際に何が有効化されているか確認します。
- `title` 列を読み、各オプションの人間向け説明を把握します。
- `depends` 列を読み、オプションが*なぜ*存在するかを理解します — 多くのシンボルは他のシンボルに引き込まれているため、子ではなく親を無効化することが正しい場合が多いです。

必要なら、二度と触らなくなったベースラインシートを固定して、後で出発点と比較できるようにしましょう:

```sh
python3 kcm.py dump -o kernel.xlsx --sheet baseline
```

## ステージ2 — 学んだことを注釈する

設定をレビューしながら、重要なオプションの「なぜ」を記録します。`memo` 列を Excel で直接編集し、テキストの正に永続化してコミットします:

```sh
python3 kcm.py memo-split --csv kernel.xlsx -o memos.csv
git add memos.csv && git commit -m "annotate baseline config"
```

例:

| name                 | memo                                 |
|----------------------|--------------------------------------|
| `CONFIG_DEBUG_INFO`  | クラッシュダンプ用に保持                 |
| `CONFIG_NF_CONNTRACK`| ファイアウォールルールの要件       |
| `CONFIG_USB_GADGET`  | 板載 USB-OTG ドライバに引き込まれたもの |

`memo-split` は現在の設定に存在するシンボルのみを書き出すため、`memos.csv` はもう存在しないオプションの注釈を持ちません。ここから `memos.csv` がメモの永続的な置き場となり、ワークブックの `memo` 列はそれらを編集する手段にすぎません。

ヒント: 設定がすでに変更されてから根拠を思い出すより、早い段階で (まさにここで) 注釈しておきます。

## ステージ3 — 最初の変更: 攻撃対象範囲を削減する

変更はカーネルツリー (ワークブックではなく) で行い、結果を設定リポジトリへ戻してコミットします:

```sh
cp config ../linux/.config            # 正 (source of truth) からツリーにシード
cd ../linux
make menuconfig          # 未使用のドライバ/ファイルシステム/ネットワーク等を無効化
make olddefconfig        # その結果生じた依存を解決
cd ../kernel-config
cp ../linux/.config config            # 結果を設定リポジトリへ戻す
```

設定リポジトリ内ディスク上の設定は*新しい*状態となり、`HEAD` にコミット済みのものが*ベース*です。変更を1つの冪等 (idempotent) ステップで記録**して**コミットします。`commit` は作業中の設定を `HEAD` と diff し、表示を再構築し、メモを更新 (`kernel.xlsx` でつけた注釈をオーバーレイ)、履歴にラベル付きのバッチを追加し、テキストソースを git コミットします — これによりベースコミットが刻まれ、設定・履歴・メモが1つのコミットで一緒に渡されます:

```sh
python3 kcm.py commit --note "攻撃対象範囲: 未使用のファイルシステム/ドライバ/ネットワークを無効化"
```

(`.kcmrc` がある場合、他のフラグは不要。なければ `--config config --base HEAD:config --srcdir ../linux --memo memos.csv
--history history.csv -o kernel.xlsx` を渡す)。設定が再び動くまで再実行は無操作 (no-op) です。`--dry-run` でプレビューできます。

何が起きたか:

- `kernel.xlsx` の `config` シートは新しい設定に一致します。`diff` 列には今回の変更 (`old -> new`、`+v`) が表示されます。削除されたシンボルは履歴に記録され、行としては保持されません。
- バッチが `history.csv` **と** `History` シートの両方に追加され、全行が同じ `batch` id、実行タイムスタンプ、注釈を共有します。バッチの `base_commit` は `HEAD` から刻まれます。
- `config`、`history.csv`、(あれば) `memos.csv` が、コミットメッセージにバッチ id とベースコミットを添えて一緒にコミットされます。(ワークブックはあなたの表示としてローカルに書き出されますが、`--commit-xlsx` を渡さない限りコミットされません。)

代わりに2ステップで行う場合 — 記録してから自分でコミット:

```sh
python3 kcm.py diff-merge --base HEAD:config --new config \
  --srcdir ../linux --memo memos.csv --history history.csv \
  --history-note "..." -o kernel.xlsx
git add config history.csv && git commit -m "攻撃対象範囲を削減"
```

`git diff config` は値の変更を正確に表示し、`history.csv` がラベル付きの監査証跡です。

## ステージ4 — 新しい要件が来る

ループを繰り返し、その要件の根拠をメモに記録します。

1. ツリーで変更する: シード (`cp config ../linux/.config`)、`make menuconfig && make olddefconfig`、そのあと `config` をコピーして戻す。
2. この要件が関わるシンボルを注釈する: `kernel.xlsx` でそれらの `memo` セルを編集 (例: `CONFIG_CRYPTO_FIPS` → `REQ-1234: FIPSモードに必須`)。
3. 注釈に要件名を入れて、1ステップで記録してコミットする。`commit` はワークブックから注釈を取り込み、`memos.csv` を更新し、履歴バッチを追加し、テキストソースをコミットします:

   ```sh
   python3 kcm.py commit --note "REQ-1234: FIPS暗号化サポートを有効化"
   ```

設定が要件を反映し、`history.csv` がラベル付きの変更履歴全体を表示し、`memos.csv` が自明でない選択を説明します。

---

## バージョン管理を維持する

- **3つのテキストファイルが状態**: `config` (値)、`memos.csv`
  (注釈)、`history.csv` (監査証跡) — すべて設定リポジトリ内。
  `git` 上で完全かつ最新に保ちます。`kernel.xlsx` は破棄可能。
- **`menuconfig` セッションの後には必ず変更を記録する** — ツリーの
  `.config` をリポジトリへコピーし、`kcm commit --note "..."` (1ステップ: 表示 +
  メモ + 履歴 + git コミット) または2ステップの `diff-merge` を実行。表示を更新し履歴バッチを追加する同期ステップです。
- **`diff` 列は一時的** (直近の merge のみ)。`History`
  シート / `history.csv` が永続的な記録。
- **いつでもコミット済みのソースから表示を再構築**可能:

  ```sh
  python3 kcm.py dump -o kernel.xlsx     # .kcmrc が config, srcdir, memo, history を供給
  ```

- **Excel なしで監査証跡を読む**: `kcm history log` (バッチごとに1行、
  新しい順) と `kcm history show <batch>` (バッチが触れた全シンボル。そのバッチ
  だけの git 適用可能な diff には `--patch` を追加)。
- **変更をいくつかの方法でレビュー**: `git diff config` (値の変更)、
  `kcm history log` / `kcm history show <batch>` (ラベル付きの監査証跡)、
  `kcm diff --base <old> --new <new> --srcdir ../linux` (タイトル付きレポート)、または
  `kcm diff --base HEAD:config --new config --patch` (レビュー / PR 用の git
  適用可能なパッチ)。
- **固定したベースラインといつでも比較**:

  ```sh
  python3 kcm.py diff --base kernel.xlsx --base-sheet baseline \
    --new kernel.xlsx --new-sheet config
  ```

- **現在のデルタだけをエクスポート**して、別々にレビュー/バージョン管理:

  ```sh
  python3 kcm.py diff-split --csv kernel.xlsx -o changes.csv
  ```

- **履歴は追記専用**で、`history.csv` (とシート) にあります。
  `--no-history` で実行をスキップ、無操作 (no-op) の実行は何も追加しません。
- **カーネルツリーのアップグレードは設定履歴と分離**: `srcdir` を新しいツリーに向ける
  (`.kcmrc` の `srcdir` 行を編集、または `--srcdir` を渡す)、リポジトリの `.config`
  からツリーにシード、`make olddefconfig`、コピーして戻し、`kcm commit` —
  追加/削除/変更されたシンボルを履歴に記録し、新しいメタデータで表示を再構築します。
  一方、設定リポジトリ自身のコミット履歴は変更されません。

## ベースは単なるテキスト設定

`diff-merge --base` は任意のテキスト `config` を受け取れます — ファイルパス、git
リブ (`HEAD:config`、`<ref>:config`)、または変更前に保存したコピー。git リブを
直接渡す (一時ファイル不要) — これがデルタを git ネイティブにするポイントです。
コミット済みの2つの設定を diff し (陳腐化しうるワークブックではなく)、バッチは
`base_commit` にベースコミットを記録して監査証跡に残します。これらはすべて
**設定リポジトリ**で行われます: `git show` は `<rev>:config` を設定リポジトリの
履歴に対して解決し、カーネルツリー (および `srcdir`) の場所とは独立です。
