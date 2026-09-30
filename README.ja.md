# kcm — Linux カーネル設定管理ツール

`kcm.py` は、カーネルの `config` を CSV にダンプし (Kconfig ツリーからメタデータを解析して付加)、Excel などの表計算アプリで見たり編集したりできるようにする。2つの `config` ファイルの差分をレポートし、設定ごとのメモ (注釈) と変更履歴を、ダンプをまたいで保持する別々の CSV で管理する。`commit` コマンドはコミット済み状態から作業中状態への変更を1ステップで記録して git コミットし、`history` コマンドは監査証跡を整形して出力する。`.kcmrc` プロジェクトファイルがパスを供給するため、一般的なコマンドはフラグ不要にできる。

## 必要条件

- Python 3.9+
- [kconfiglib](https://github.com/ulfalizer/kconfiglib) >= 14
- [openpyxl](https://openpyxl.readthedocs.io/) >= 3.1 (`.xlsx` ファイルのみ)

```sh
pip install -r requirements.txt
```

## CSV 形式

`dump` コマンドは、`config` に見つかった設定オプションごとに1行を出力する (`config` の順序どおり、値が `n` の `# CONFIG_FOO is not set` エントリを含む):

| 列        | 意味                                                        |
|-----------|----------------------------------------------------------------|
| `name`    | `CONFIG_` プレフィックス付きのオプション名                                 |
| `type`    | Kconfig の型: `bool`、`tristate`、`string`、`int`、`hex`、`choice` |
| `title`   | Kconfig のプロンプト文字列 (例: `Local version - append to kernel release`) |
| `value`   | `config` からの値 (`y`、`n`、数値、またはクォートなしの文字列) |
| `diff`    | 他の `config` に対する差分 (`diff-merge` が記入)。ない場合は空 |
| `default` | 全 Kconfig デフォルトを `;` で連結。条件付きは `value if CONDITION` |
| `depends` | 完全な依存式 (囲む `if` メニューコンテキストを含む)。`<choice>` は「その choice が有効なときに表示される」ことを意味する |
| `memo`    | メモ CSV からの注釈、または空                           |

`config` に存在するが Kconfig ツリーにないオプション (例: 古いソースツリーに対する新しい `config`) は `type` が空になる。件数は stderr に報告される。

同じ列が `.xlsx` 出力にも使われる ([Excel ワークブック](#excel-ワークブックxlsx) を参照)。

## 設定入力: ファイルパスか git rev

`dump --config`、および `diff` と `diff-merge` の `--base`/`--new` は、それぞれ**ファイルパス**か **git rev**を受け取れる:

- ディスク上に存在するパスはそのまま使われる;
- さもなくば、その引数は `git show` で読む git rev となる:
  `HEAD:config`、`main:config`、`v6.5:config`、または単体の
  `HEAD`/`main`/`@{upstream}` (これはもう片方のオペランドからパスを借用する。
  つまり `--base HEAD --new config` は `HEAD:config` を読む)。

git は**プロジェクトルート** (発見した `.kcmrc` のディレクトリ、なければカレント
ディレクトリ) から実行されるため、相対パスと `git show` はそこから解決される
— つまりリポジトリ内のどこからでも `kcm` を実行できる。これが一時ファイルなしで git
ネイティブなワークフローを成立させる仕組みである: `HEAD` と直接 diff して記録する。

## プロジェクトファイル (`.kcmrc`)

`.kcmrc` は、プロジェクトのパスとデフォルトを一度だけ記録し、一般的なコマンドで
フラグが不要になるようにする (`.git/config` のようなもの)。`[kcm]` セクションを持つ
INI ファイルで、カレントディレクトリから**上層へたどって**発見される; その
ディレクトリがプロジェクトルートになる。優先順位は **CLI フラグ > `.kcmrc` >
ビルトインデフォルト** である。このファイルはリポジトリにコミットする (プロジェクトの設定のため)。

```ini
[kcm]
config  = config         # 作業中の設定 (--new のデフォルトもこれ)
srcdir  = ../linux       # カーネルソースツリー (Kconfig を含む)
xlsx    = kernel.xlsx    # ワークブック (dump/diff-merge/commit の出力)
memo    = memos.csv      # メモ CSV (name,note)
history = history.csv    # 履歴 CSV (監査証跡)
# base    = HEAD:config  # デフォルト; HEAD:<config> に等しい
# arch    = x86_64       # 任意: arch / cc / ld
```

キー: `config`、`srcdir`、`base`、`xlsx`、`sheet`、`memo`、`history`、任意で
`arch`/`cc`/`ld`。2つのデフォルトが自動計算される: `--new` はデフォルトで
`config`、`--base` はデフォルトで `HEAD:<config>` — したがって `.kcmrc` が
あるとき、`kcm diff`、`kcm diff-merge`、`kcm commit` はすべて「コミット済みの
設定から作業中の変更」を意味する。

.kcmrc の検索は `--rc PATH` (特定のファイルを使う) または `--no-rc` (無効化) で制御する。

標準的なレイアウトでは**設定リポジトリはカーネルソースツリーと分離**されている:
`srcdir` はツリーを指し (隣のディレクトリ、別のリポジトリ、ただのチェックアウト)、
バージョン管理されるのは設定リポジトリだけである。ガイドは
[WORKFLOW.md](WORKFLOW.ja.md#レイアウト設定リポジトリはカーネルツリーと分離する) を参照。

## コマンド

### dump

`config` とカーネルソースツリーを解析し、CSV (または `.xlsx`) を書き出す。

```sh
python3 kcm.py dump --config config --srcdir ../linux -o dump.csv
python3 kcm.py dump --config config --srcdir ../linux --memo memo.csv -o dump.csv
# テキストソース (メモ + 履歴) からワークブックの完全な表示を構築
python3 kcm.py dump --config config --srcdir ../linux --memo memo.csv \
  --history history.csv -o book.xlsx
```

| オプション     | デフォルト      | 説明                                          |
|------------|--------------|------------------------------------------------------|
| `--config` | (必須)   | `config` ファイルへのパス、または git rev ([設定入力](#設定入力-ファイルパスか-git-rev) を参照) |
| `--srcdir` | (必須)   | カーネルソースツリーへのパス (`Kconfig` を含む)  |
| `--memo`   | なし         | `memo` 列を事前埋めするためのメモ CSV               |
| `--history`| なし         | `History` シートを作る履歴 CSV (`.xlsx` のみ) |
| `--sheet`  | `config`     | 出力ワークブックのシート (`.xlsx` のみ)          |
| `-o`       | stdout       | 出力 CSV ファイル (または `.xlsx` ワークブック)                |
| `--arch`   | `x86_64`     | ターゲットアーキテクチャ (`SRCARCH` も正しく設定)  |
| `--cc`     | `$CC` または `gcc` | Kconfig の `cc-option` 検査に使う C コンパイラ    |
| `--ld`     | `$LD` または `ld`  | Kconfig 検査に使うリンカ                     |

`--arch`/`--cc`/`--ld` は Kconfig ツリーの解析 (例: アーキ固有の `source` パス、
`cc-option` のチェック) にのみ影響する。値は変えない (常に `config` から来る)。
`--history` は `History` シートを指定 CSV で**置換**する (追記ではなく構築)。
対応する設定オプションのないメモエントリは stderr に警告を出して無視される。

### diff

2つの `config` ファイルの差分を、人が読むためのリストとして stdout に出力する
(集計は stderr)。`--srcdir` を付けると各変更シンボルの Kconfig タイトルが追加される。

`--base`/`--new` はファイルパス、git rev ([設定入力](#設定入力-ファイルパスか-git-rev) を参照)、
またはダンプテーブル (`.csv` か `.xlsx`、拡張子で判定) を受け取れる —
例: 同じワークブックの2つのシートを比較:

```sh
python3 kcm.py diff --base config --new config.new
python3 kcm.py diff --base config --new config.new --srcdir ../linux
python3 kcm.py diff --base HEAD:config --new config --srcdir ../linux
python3 kcm.py diff --base book.xlsx --base-sheet before \
                    --new book.xlsx --new-sheet after
```

```
changed (2):
  CONFIG_KERNEL_GZIP: n -> y
  CONFIG_IKCONFIG: m -> y
added (1):
  CONFIG_RUSTC_HAS_SPAN_FILE: +y
removed (1):
  CONFIG_DECOMPRESS_ZSTD: -y
```

### diff --patch

`--patch` (または `-u`) を付けると、`diff` はレポートの代わりに2つの設定の
git スタイル **unified diff** を stdout に出力する (両オペランドはテーブルで
はなく `config` 入力であること)。集計は引き続き stderr に、終了ステータスは
設定が異なるとき `1`、同一のとき `0` を返すためスクリプトから使うことができる。出力は
`git apply` 互換 (`a/`/`b/` パスプレフィックス) である:

```sh
python3 kcm.py diff --base HEAD:config --new config --patch > change.patch
git apply --check change.patch
```

### diff-merge

(変更された) 設定に対してテーブルを更新し、変更を記録する。差分は
ベースと新しい設定の間に計算される:

- **git ネイティブ:** `--base` と `--new` で両方にテキストファイルを指定。
  出力テーブルは新しい (現在の) 設定を反映して再構築されるため、常に「今カーネルに
  あるもの」のクリーンな表示になる。
- **テーブルベース:** `--csv <table>` を指定 (このとき `--base` なし)。
  テーブルの `value` 列がベースとなり、`memo` 列は引き継がれる
  (`--memo` で上書きされない限り)。

出力テーブルは新しい設定のシンボルごとに1行で、`diff` 列に今回の変更が表示され、
(`--srcdir` を指定した場合は) Kconfig メタデータも付く。削除されたシンボルは
行として保持されない — 履歴にのみ記録される。

```sh
# git ネイティブ: 変更を記録し履歴バッチを追加
python3 kcm.py diff-merge --base config --new config.new --srcdir ../linux \
  -o book.xlsx --history history.csv --history-note "trim attack surface"
# HEAD をそのまま基準に git ネイティブで実行 (一時ファイル不要)。バッチがベースコミットを記録
python3 kcm.py diff-merge --base HEAD:config --new config --srcdir ../linux \
  -o book.xlsx --history history.csv --history-note "trim attack surface"
# メモ CSV を出力に適用
python3 kcm.py diff-merge --base config --new config.new --memo memo.csv \
  -o dump.csv
# テーブルベース (インプレース): ベースとメモは既存のテーブルから
python3 kcm.py diff-merge --csv book.xlsx --new config.new \
  -o book.xlsx --out-sheet before
```

差分セルの形式: `old -> new` (値が変更)、`+v` (新しい設定で追加)、`-v` (削除)、
空 (変更なし)。

変更がある実行のたびに**履歴バッチ**として記録される ([履歴シート](#履歴シート) を参照):
`.xlsx` 出力では `History` シートに、`--history` CSV にも追記される。
`--no-history` は何も記録せず、`--history-note TEXT` は全エントリに注釈を記録する。
`--base` と/または `--new` が git revの場合、バッチは対応するコミットハッシュを
`base_commit`/`new_commit` に記録する (入力が未コミットのワークツリーか単なる
パスの場合は空)。対応する設定オプションのないメモエントリは stderr に警告を出して
無視される。

### diff-split

注釈付き CSV から空でない `diff` セルを取り出し、単体の差分 CSV (`name,diff`
ヘッダ) にする。記録された差分を別に保持/バージョン管理することもできる。

```sh
python3 kcm.py diff-split --csv dump-diff.csv -o diff.csv
```

### memo-merge

既存のダンプ CSV の `memo` 列をメモ CSV から埋める。両方に注釈がある場合は
メモファイルが優先。

```sh
python3 kcm.py memo-merge --csv dump.csv --memo memo.csv -o dump-annotated.csv
```

### memo-split

注釈付き CSV から空でない `memo` セルを取り出し、単体のメモ CSV (`name,note`
ヘッダ) に戻す。これがダンプ間でメモを保持する方法。

```sh
python3 kcm.py memo-split --csv dump-annotated.csv -o memo.csv
```

### history-split

ワークブックの `History` シートを取り出し、履歴 CSV
(`batch,date,base_commit,new_commit,sheet,name,diff,note`) に戻す。
`dump --history` の逆で、ワークブックから変更履歴を保持する方法。

```sh
python3 kcm.py history-split --csv book.xlsx -o history.csv
```

### commit

記録してコミットするループを1つのステップで行う: 作業中の設定をベース
(デフォルト `HEAD:<config>`) と diff し、ワークブック表示を再構築し、メモ CSV を
更新し、履歴バッチを追加し、テキストソースを `git add`/`git commit` する。
`.kcmrc` があるときは --note だけで済む:

```sh
# .kcmrc がある場合 (config/srcdir/memo/history はすべてそこから)
python3 kcm.py commit --note "trim attack surface"
# 完全に明示的に
python3 kcm.py commit --config config --base HEAD:config \
  --srcdir ../linux --memo memos.csv --history history.csv -o kernel.xlsx \
  --note "REQ-1234: enable FIPS crypto support"
```

動作:

- **変更なしの場合** (設定がベースと一致) → 通知を表示して `0` で終了し、何も触らない
  (再実行しても安全)。
- **変更がある場合はマスター情報 (source of truth)** をコミットします — `config`、`history.csv`、
  (メモがあれば) `memos.csv`。コミットメッセージは `--note` (または
  `Update <config>`) に、grep 可能なトレーラーを付ける:
  ```
  kcm-batch: 3f9a2c1d8b4e
  kcm-base:  26d4c7f…
  kcm-delta: 13 changed, 5 added, 1 removed
  ```
- ワークブックはパスが設定されている場合 (`-o`/`.kcmrc` の `xlsx`) に**ローカルに
  書き出される** (現在の表示として) が、デフォルトでは**コミットされない** —
  派生バイナリだからである。ステージして一緒にコミットするには `--commit-xlsx` を付ける。
- `--no-git` はメモ/履歴/ワークブックを書き出して `git` をスキップ。`--no-memo`
  はメモ更新をスキップ。`--dry-run` は書き出さず/コミットせずに変更とファイルだけ
  報告。`--signoff`/`-s` は `Signed-off-by` を追加。

### history

`history.csv` から変更履歴を整形して出力する。`history` 単体はバッチを
一覧表示 (`git log` のよう)、`show` は1つを展開する。

```sh
python3 kcm.py history log                 # バッチごとに1行、新しい順
python3 kcm.py history log --limit 5 --long
python3 kcm.py history show 3f9a2c         # 1バッチ: シンボルごとの old -> new
python3 kcm.py history show 3f9a2c --patch # + バッチの git unified diff
python3 kcm.py history log --json
```

`log` は `<batch>  <date>  base=<sha7>  new=<sha7>  <n>c <a>a <r>r  <note>`
を表示する。`--reverse` で古い順、`--long` でバッチごとにブロック表示、`--json`
でクリーンな機械可読出力。`show <batch>` は完全な、または一意なプレフィックスの
バッチ id を取り、各変更シンボルをその `diff` セル付きで一覧表示する (これは
git を必要としない)。`--patch` はバッチを git unified diff として stdout に
再構築する (base = `<base_commit>:<config>`、new = `<new_commit>:<config>`
または `HEAD:<config>`; `--base`/`--new` で上書き)、人が読む用の出力は stderr に
移動してパッチをクリーンに保つ。

## 一般的なワークフロー

カーネル設定を Excel ワークブックとして構築・維持する (ベースライン、注釈、
変更トラッキング) ガイドは [WORKFLOW.md](WORKFLOW.ja.md) を参照。
最小の CSV ラウンドトリップは:

```sh
# 1. 最初のダンプ (まだメモなし)
python3 kcm.py dump --config config --srcdir ../linux -o dump.csv

# 2. dump.csv を Excel で開き、memo 列にノートを書いて保存
#    (kcm が読み戻せるようファイルを CSV のままに)

# 3. ノートをcsv化
python3 kcm.py memo-split --csv dump.csv -o memo.csv

# 4. カーネルソースか設定が変わって再ダンプしても、メモがcsvから埋まる
python3 kcm.py dump --config config --srcdir ../linux --memo memo.csv -o dump.csv

# 5. またはツリーを再解析せず、既存のダンプにメモをマージ
python3 kcm.py memo-merge --csv dump.csv --memo memo.csv -o dump-annotated.csv
```

メモ CSV の形式は単純な2列である。`CONFIG_` プレフィックスは任意、大文字小文字は
厳密に比較される:

```csv
name,note
CONFIG_DEBUG_INFO,keep enabled for crash dumps
LOCALVERSION,set to -local for vendor builds
```

メモ CSV 内の重複名がある場合は最後のエントリが優先 (警告が表示される)。

## 設定の差分を取る

`config` を再生成した後 (カーネルツリーで `make menuconfig` を実行し、ツリーの
`.config` を設定リポジトリへコピー)、`HEAD` のコミット済みバージョンがベース、
作業中のファイルが新しい状態になる。git と直接 (一時ファイルなしで) 変更を
確認して記録する:

```sh
# 1. 差分を確認
python3 kcm.py diff --base HEAD:config --new config --srcdir ../linux

# 2. 差分を記録: テーブルを再構築、diff 列を記入、履歴バッチを追加
#    (base_commit は自動で記録される)
python3 kcm.py diff-merge --base HEAD:config --new config \
  --srcdir ../linux -o book.xlsx --history history.csv --history-note "..."

# 3. 変更を git 適用可能なパッチとして出力 (レビュー / PR 用)
python3 kcm.py diff --base HEAD:config --new config --patch > change.patch

# 4. 現在の差分だけを単体ファイルとして保持
python3 kcm.py diff-split --csv book.xlsx -o diff.csv
```

ステップ2と `git commit` は1つのコマンド — `kcm commit --note "..."`
([commit](#commit) を参照) — に統合される。`.kcmrc` があるときは
`kcm commit --note "..."` だけで、メモ CSV も更新する。

## Excel ワークブック (.xlsx)

すべてのテーブルファイル (`.csv`) は `.xlsx` ワークブックにもでき、拡張子で
判定される。これらには `openpyxl` が必要である。一般的なワークフローでは、
プロジェクトごとに1つのワークブックを設定スナップショットごとに1シートの形で
保つ:

```sh
python3 kcm.py dump --config config --srcdir ../linux -o book.xlsx --sheet before
# ... 設定を変更 ...
python3 kcm.py dump --config config.new --srcdir ../linux -o book.xlsx --sheet after
python3 kcm.py diff --base book.xlsx --base-sheet before --new book.xlsx --new-sheet after
python3 kcm.py diff-merge --csv book.xlsx --sheet before --new config.new \
  -o book.xlsx --out-sheet before
python3 kcm.py memo-split --csv book.xlsx --sheet after -o memo.csv
```

シートの扱い:

- **書き込み** (`-o book.xlsx`): 新しいワークブックには1つのシート、既存の
  ワークブックは他のシートをすべて保持する。シート名はデフォルトで `config`
  (`dump` は `--sheet`、他は `--out-sheet`)。その名前の既存シートは置換される。
  新しいシートは末尾に追加される。
- **読み込み** (`--csv book.xlsx` / `--base` / `--new`): シートは `--sheet`
  (または `diff` では `--base-sheet`/`--new-sheet`) で指定。指定しなければ
  ワークブックの**最初の**シートが使われる。
- `kcm` が書くシートには太字のヘッダ行、1行目のフリーズ、オートフィルタ、調整済み
  列幅が付けられる。全セルはテキストとして書き出される。
- `.xlsx` 出力には `-o` が必要 (stdout には出力できない)。

注意: セルはテキストとして読み戻されるため、値はラウンドトリップしても保持される —
しかし Excel UI で*再入力*した値は数値として保存されるかもしれない (整数なら
問題ない、先頭ゼロの文字列は落ちる)。`kcm` 経由の保存は他のシートのデータと
基本書式を保持するが、特殊な内容 (チャート、マクロ、カスタム描画) は openpyxl
によって劣化することがある。

## 履歴シート

`diff-merge` は各実行の変更を**バッチ**として記録する: 変更シンボルごとに1行、
すべてが同じ `batch` id、タイムスタンプ、`--history-note`、そして — 入力が git
revの場合は — ベース/新しいコミットハッシュを共有する。バッチはワークブックの
`History` シート (`.xlsx` 出力の場合) と/または履歴 CSV (`--history`) に追記される:

| 列            | 意味                                                        |
|---------------|----------------------------------------------------------------|
| `batch`       | 実行ごとの id (12桁の16進トークン); 実行の全行が共有 |
| `date`        | `diff-merge` が実行された時刻 (シートでは Excel 日時; ローカル時間) |
| `base_commit` | `--base` の出典 git コミット (完全な SHA; git revでなければ空) |
| `new_commit`  | `--new` の出典 git コミット (完全な SHA; 未コミットのワークツリーか非rev入力は空) |
| `sheet`       | 更新されたテーブルのシート                               |
| `name`        | `CONFIG_` シンボル                                           |
| `diff`        | diff 列と同じ形式: `old -> new`、`+v`、`-v`       |
| `note`        | `--history-note` から、または空                                |

コミット列は各バッチを `git` と結びつける: `history show <batch> --patch` が
バッチを git パッチとして再構築する (または `diff --base <base_commit>:config
--new <next>:config --patch` で手動実行)。`history log` (一覧) と `history show
<batch>` (1バッチのシンボルごとの変更) で Excel なしに証跡を読める —
[history](#history) を参照。

履歴は設定変更の永続的な記録である: データシートは再ダンプで置換されるが、履歴は
されない。`History` シートは初回使用時に作成され、それ以降は増えるだけである
(実行ごとにバッチ1つを追加)。変更のない実行は何も追加せず、`--no-history` は
記録自体をスキップする。

履歴の**CSV がマスター情報 (source of truth)** である
([WORKFLOW.md](WORKFLOW.ja.md) を参照)。`dump --history FILE` はそれからの
`History` シートを再構築し、`history-split` が取り戻すため、ワークブックの履歴は
CSV の再生成可能な表示である。データシートに `History` という名前は使えない:
データシートにその名前があると `diff-merge`/`dump` は履歴の記録/構築を拒否する。

## 備考

- Kconfig ツリーは `warn=False` で解析される。解析が失敗した場合は、`--srcdir`
  が完全なカーネルツリーを指していること、`--arch`/`--cc`/`--ld` がシステムにある
  コンパイラと一致することを確認すること。
- カーネル 7.x は kconfiglib がまだ知らない Kconfig 構文を導入している
  (`transitional`、`modules`、条件付きの `depends on X if Y`)。`kcm` はこれらを
  書き換える: `depends on X if Y` は等価な `(!Y || X)` になり、
  `depends` 列に現れることがある。
- choice のメンバー (例: `CONFIG_HZ_250`) は型が `choice` で `depends = <choice>`
  になる。choice 自体 (例: `CONFIG_HZ`) はそのメンバーの条件付きデフォルトを
  表示する。
- すべての進行/警告メッセージは stderr に出力されるため、`-o` を省略すると
  stdout は常にクリーンな CSV になる。
