# Maya ポータブル・レンダーファーム

Windows の Maya 2025 / 2026 / 2027 と Arnold を使う、共有フォルダ型のレンダーファームです。複数PCが同じキューから別々のジョブを取得します。USBに梱包したコピーは、ネットワークのないPCでも独立したキューとして動作します。GUIは日本語、操作用のWebサーバーやデータベースは不要です。

このREADMEは**現行実装の操作手順**です。試験内容と未検証項目は [VALIDATION.md](VALIDATION.md)、GitHub公開時の除外対象と開発手順は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

## 1. 必要なものと起動

- Windows PCに Maya 2025、2026、2027 のいずれかと Arnold（MtoA）をインストールします。ジョブ投入時のMaya版指定は既定で「指定しない」です。未指定なら対応版を持つPCが取得し、初回取得後は途中画像の混在を防ぐため、そのジョブの実行版を維持します。プラグインはインストールと読み込み可否を確認し、バージョンの一致は要求しません。GPUレンダーには対応するGPU・ドライバー・Arnoldの利用環境も必要です。
- ファームのフォルダを、使うPCすべてが読み書きできる共有先に置きます。USB運用ではUSB上の梱包先を使います。ドライブ文字に依存しないよう、プログラムは起動ファイルの場所を基準にパスを解決します。
- [起動.cmd](起動.cmd) をダブルクリックします。[start.cmd](start.cmd) も同じです。通常はインストール済みの最新対応版の `mayapy.exe` と付属PySide6でGUIが開きます。特定版のGUIを使うなら `start.cmd -MayaVersion 2025` のように指定します。非標準のインストール先は「設定」でPCごとに指定できます。
- 「PCの状態 → 環境診断」で利用可能なMaya、Arnold、プラグイン、画像検証ツール、後処理環境を確認します。これはシーンの全レンダリング試験ではありません。診断の実装は [farm/worker.py](farm/worker.py) の `diagnose`、Maya側は [farm/maya_entry.py](farm/maya_entry.py) です。

GUIとワーカーは別プロセスです。**GUIを閉じてもワーカーとレンダリングは続きます。** ワーカーを起動するには「PCの状態 → このPCで処理開始」を押します。2台目以降も同じ共有フォルダから起動し、各PCで同じ操作をします。停止は「このPCをフレーム後に停止」を使い、PC状態に停止が表示されたことを確認します。キューが空でもワーカーは次の投入を待ちます。

プログラムを更新した場合は、ダッシュボードを開き直し、各PCのワーカーも安全に停止して起動し直してください。既に動作中のワーカーは読み込んだ旧コードのまま処理を続けます。

## 2. Mayaプロジェクトの配置と登録

推奨はファームルート内の `projects/<作品名>/` に、`workspace.mel`、`scenes/`、`sourceimages/`、参照シーン、キャッシュ等を**Mayaプロジェクトの構成のまま**配置する方法です。別の共有フォルダの既存プロジェクトを直接登録することもできます。その場合はすべてのワーカーPCから同じ素材へアクセスできるようにしてください。`data/` と `workers/` にプロジェクトを置かないでください。

```text
mur_render_farm/
  起動.cmd
  projects/
    作品A/
      workspace.mel
      scenes/c071/c071.mb
      scenes/c071/History/c071_20260913.mb
      sourceimages/
      assets/
```

「シーン一覧 → プロジェクトを登録」で **`workspace.mel` のあるフォルダ**とMaya版を選択します。配置や登録だけではシーン一覧の調査・キュー投入は行いません。登録後、「更新を確認」を押してください。プロジェクト外の特殊素材は「素材の梱包 → プロジェクトに追加素材フォルダを登録」で指定できます。登録解除は「シーン一覧 → プロジェクト登録を解除」を使い、元ファイル・画像・ログを消さずに登録と待機ジョブを取り消します。実行中ジョブがある場合は停止を確認してから解除します。

異なるPCで保存された古い素材パスは、登録先と同名のプロジェクトルートや `sourceimages` 内の一意な候補を用いて、読み込み中のMaya上で解決します。明示的な対応は**プロジェクト直下**の `asset_paths.json` に書けます。形式は [asset_paths.example.json](asset_paths.example.json) を参照してください。元シーンは上書きしません。解決できないレンダー用テクスチャ等は `asset_paths_report.json` とMayaログに警告を残して続行するため、画像の品質を確認してください。参照シーンの読み込み失敗や画像検証失敗は停止対象です。USB梱包の詳細調査では不足素材を検査し、必要素材が解決できない場合は梱包を完了扱いにしません。

## 3. シーン一覧の更新とジョブ投入

シーン一覧の**シーンファイル調査は自動実行しません**。GUI起動時は保存済み一覧を表示します。「更新を確認」で、`scenes/` 直下と各シーンフォルダ直下の `.ma` / `.mb` を列挙し、`History`（大文字・小文字を区別しない）やその他の下位フォルダは探索しません。同じシーン候補の中から**Windowsのファイル更新日時が最新**のものを選びます。名前に書かれた日付は選択基準ではありません。更新日時・サイズが保存済みと同じ採用ファイルはシーン読み込み結果を再利用します。「シーン情報を強制再取得」は採用シーンを状態にかかわらずMayaで読み直します。調査中の登録・再調査操作は無効になり、進捗・工程・経過時間を表示します。

一覧調査はMaya付属の `mayapy.exe` / `maya.standalone` でシーンを開きます。参照の深い読み込みやシーンのスクリプト実行を抑え、再生・アニメーション全体・Render Settingsの範囲と刻み、時間単位/FPS、解像度、カメラのrenderable属性、レイヤー/AOV、警告を `data/state.json` に保存します。一覧取得ではプロジェクト全ファイルをハッシュしません。参照内だけにあるカメラなど、軽量調査で得られない情報があります。

シーンを選んで「選択シーンを投入」を押します。**投入時はファイル更新を再調査せず、保存済みのシーン情報を使用します。** 元ファイルの変更後は先に「更新を確認」を押してください。設定画面でMaya版（既定は指定しない）、カメラ、レイヤー、AOV、出力形式、解像度、AA、CPU/GPU、OIDN、タイムアウト、後処理を確認します。Maya版を指定しない場合は、シーン調査時の版を優先し、利用できなければ別の対応版を選びます。シーンの互換性は実際の読み込み時にも確認されます。既定のフレーム範囲は**再生範囲**です。整数フレームを指定します。複数シーンを選択すると、シーンごとに設定画面が開きます。投入後は「PCの状態」でワーカーを起動してください。

シーンの更新を検出すると新しい版を「更新あり・未レンダリング」として記録します。最新版は**自動投入されません**。待機中の旧版ジョブは保留し、実行中の旧版はそのまま完了しますが、最新版の完了扱いにはしません。共有運用の通常ジョブは、登録したプロジェクトのシーン・素材を**直接使用**します。投入時のスナップショットやプロジェクトコピーは作りません。レンダリング中に元素材を編集すると、次の読み込み・再開時に変更後の内容が使われ得るため、投入中は元プロジェクトを固定してください。

## 4. キュー、PC状態、停止と再開

「ジョブキュー」で順序変更、保留/停止、再開、再試行、削除、作業領域・画像フォルダの表示ができます。待機ジョブの削除はすぐ反映され、実行中のジョブは担当Mayaを停止した後に除外されます。記録と既存画像は残ります。「再開」は保留/停止したジョブ、「再試行」は失敗ジョブ等を待機へ戻す操作で、再試行では自動再起動回数も初期化します。旧版シーンのジョブは再開せず、最新版を投入します。

「PCの状態」は担当ジョブ、工程、完了数/総数、現在のフレーム、経過時間、シーン読込時間、最長フレーム時間を表示します。画面上の状態は約2秒ごとに更新します。1台が1ジョブ全体を担当し、所有トークン・共有ロックで重複処理を防ぎます。監督プロセスがMayaと別にハートビートを送ります。所有権が無効になればそのジョブを離れてキューを読み直します。ハートビート期限切れだけでは別PCへ割り当てません。

最後のハートビートから45秒を超えたPCは「更新なし（最終記録: …）」と表示します。そのPCが所有する実行中ジョブも「更新なし（最終記録: …）」として数え、現在動いているレンダリング件数には含めません。通信断や長い環境診断中にもこの表示になり得るため、「更新なし」はプロセス停止の確定ではありません。実際のPC・Maya・ワーカーロックを確認してから復旧操作を行ってください。画面表示だけを変え、所有権・ジョブ状態を自動変更しません。

フレーム画像一式が完成しない時間の既定上限は**30分**、同一フレームのMaya再起動は**3回**です。ログ更新や不完全画像では時間をリセットしません。シーン起動・読み込みの上限は**60分**で、画像未生成の判定とは別です。タイムアウト時はそのジョブのMayaと子プロセスを止め、検証済みフレームを再利用して続行します。画像を再利用するときは、再びMayaでシーンを読み込み、記録された設定・画像の内容を検証します。通信・保存先障害は回復を待ちます。

ワーカーPCが異常終了した場合は、まずそのPCのMayaとワーカーの停止状況を確認し、同じPCで再起動してください。別PCで続ける場合は元PCのワーカー・Mayaが停止したことを確認してから「停止済みPCのジョブを回収」を使い、保留に戻して「再開」します。別PCへ自動横取りしません。コードを更新した場合、動作中のワーカーは古いコードを保持するので、安全に停止してから再起動します。GUIだけの変更はGUIを閉じて `start.cmd` で開き直します。

### 失敗ジョブの後処理だけを変更する

ジョブキューで**1件**選び「後処理設定を変更」を押します。「FastDVDnet sigma 10でデノイズ」と「FFV1動画を作成」は個別に切り替えられます。FastDVDnetで失敗した場合は、FastDVDnetを解除して保存し、次に「再試行」を押してください。設定の保存だけでは実行しません。待機・保留・停止・エラーのジョブだけ変更できます。実行中なら先に停止完了を確認してください。保存済みの検証済み画像は保持します。完了済みジョブへの追加処理には「後から動画化・デノイズ」を使います。

## 5. 作業領域、画像、動画の保存先

```text
mur_render_farm/
  farm/                         実装
  farm_cli.py                   GUI/CLI入口
  launch.ps1, 起動.cmd, start.cmd
  projects/                     任意のプロジェクト置き場
  data/state.json               プロジェクト、シーン、キュー、ジョブ履歴、PC状態
  data/state.lock               複数PC操作の排他
  data/blobs/                   USB梱包用の内容ハッシュ付き素材キャッシュ
  workers/<PC名_一意ID>/
    config.json                  このPCの設定
    capabilities.json            このPCの環境診断結果
    probes/                      mayapyによるシーン調査ログ等
    jobs/<ジョブID>/
      request.json               Mayaへの実行指示
      frames.json                検証済みフレーム記録
      raw/                       検証済み途中画像
      rendering/                 生成・検証中の画像
      quarantine/                不完全・破損画像の隔離
      maya_status.json           Mayaからの進捗
      logs/                      Mayaログ
      processed/                 公開前の後処理成果物
    postprocess/                 完了後の追加処理の作業領域
    dashboard/                   HTML/連結動画作成中の作業領域
    tmp/                         Maya・Python等の一時ファイル
  staging/                       公開前の検証領域
  results/
    <シーン名>/raw/              公開済みBeauty・AOV連番
    <シーン名>/processed/        投入時の後処理結果
    <シーン名>/postprocess/      完了後に追加した後処理結果
    dashboard.html               サムネイル・動画ページ
    dashboard/<日時>/            ページ用MP4、サムネイル、連結MP4
  bin/ffmpeg.exe                 任意の動画化ツール
  bin/WinPython/                 任意のFastDVDnet用Python
  tools/fastdvdnet/              任意のFastDVDnetコード・モデル
  tests/runtime/                 テスト用データ・過去の検証結果
```

PC名にMachineGuid由来のIDを付け、同名PCの衝突を防ぎます。ツール管理の作業領域、Maya用一時ディレクトリ、後処理キャッシュはこのルート内です。Maya本体やライセンス機構の既存管理領域は対象外です。`workers/.../jobs/<ジョブID>` は中断再開に必要なので、処理中は移動・削除しないでください。

新規ジョブID、追加後処理ID、Maya起動回ID、シーン調査ID、公開前の一時IDは16桁の16進数です。既存の32桁IDは変更しません。後処理の作業用グループ名は `g01` 等に短縮し、レイヤー・カメラ名は結果の検証記録に保持します。Windows/UNCのパス文字数を抑えるため、JSON・素材コピーの一時ファイル名も短縮しています。所有権トークン、内容ハッシュ、フレーム識別子は検証・再開の整合性に必要なため、長さを変更しません。変更をワーカーに適用する際は、進行中ジョブの安全な停止とワーカーの再起動が必要です。

レンダーのBeautyはPNG/EXR、AOVはEXRを使います。新規の検証済み画像は `raw/` 直下に、レイヤー・カメラ識別子と**6桁のフレーム番号**を含む名前で置きます。途中画像は`rendering/`、破損・中断画像は`quarantine/`です。公開前に再検証し、`results/<シーン名>/raw/` に保存します。再レンダリング等で同名結果がある場合は日時・連番付きの別シーンフォルダにし、既存結果を上書きしません。ジョブキューの「画像フォルダを開く」で、完了時は公開先、実行中・停止・失敗時は作業領域の画像を開けます。

`data/state.json` は手で編集せず、GUIかCLI/APIで操作してください。共有フォルダからの同時操作は排他下で処理し、古い画面のrevisionからの変更を拒否します。`tests/runtime` は本番処理から参照されません。削除すると通常運用には影響しませんが、手動で作ったテスト用スクリプト・素材・過去の検証記録は自動再作成されません。削除はテストや移行スクリプトが動いていない時に行ってください。`projects/`、`data/`、`workers/`、`results/`、`staging/` は運用データです。

## 6. 任意の後処理と結果HTML

既定は追加処理なしです。投入時にFFV1動画化やFastDVDnet sigma 10を選べます。PNG Beauty連番が対象で、AOV原本を残します。FFmpegを `bin/ffmpeg.exe` に置くと未指定時に自動検出します。FastDVDnetはMaya付属Pythonとは別の `bin/WinPython/<展開フォルダ>/python/python.exe` と `tools/fastdvdnet/models.py`、`fastdvdnet.py`、`model.pth` を使用します。[tools/setup_fastdvdnet.py](tools/setup_fastdvdnet.py) が導入用です。大容量のPython・モデル・実行ファイルはGitHub公開対象から除外されています。USB梱包ではこの任意環境を自動収録しないため、USB側に別途配置して環境診断してください。

後処理依存環境が不足すると後処理は失敗し、元画像とログは残ります。FastDVDnetの依存環境読み込みが60秒を超えた場合は診断結果に記録します。動画だけを指定したジョブではFastDVDnetの依存環境を診断しません。完了ジョブは「後から動画化・デノイズ」で公開済みPNG Beautyから処理でき、Maya再レンダリングは不要です。追加結果は同じシーン結果フォルダの `postprocess/<日時>/` に保存し、「後処理結果を開く」で表示できます。投入時の結果は `processed/<日時>/` に保存します。EXR Beautyの追加後処理は未対応です。

シーン一覧の「結果HTML・全動画を連結」は、`results/dashboard.html` と `results/dashboard/<日時>/` のH.264 MP4・サムネイル・シーン名テロップ付き `combined.mp4` を**手動で作成**します。シーン名の自然順で並べ、既存の検証済み動画や手動配置したPNG/EXR連番を対象にします。元画像・元動画・ジョブ記録は変更しません。HTML内の相対パスで動画を再生するので、移動時は `results/` 全体を保ってください。FFmpegと日本語表示用フォントが必要です。

他ファームの連番は `results/c020/raw/beauty.000001.png`、`beauty.000002.png` のように置き、連番FPSを設定して作成ボタンを押します。EXRも対応します。シーン別FPSは `results/c020/sequence.json` の `{"fps":"24"}` を優先します。既存動画があれば同じシーンのrawから重複して動画化しません。HTML用の `results/dashboard/` に原本は置かないでください。未検証・検証失敗と明記された成果物は対象外です。初回生成後に結果が増えたらボタンを再度押してください。

## 7. USBへ梱包

「素材の梱包」で持ち出すシーンを選び、**空の保存先フォルダ**を指定します。開始時は選択シーンだけをMayaで詳細調査し、必要な外部素材・不足素材・プラグインを確認します。その後、梱包用素材をハッシュ付きキャッシュに取り込み、プロジェクト全体と検出された外部素材をコピーします。つまり、シーン詳細の調査だけで梱包する素材を最小限に限定するわけではありません。コピー中の変更、保存先容量、FAT32の4GiB制限を検査します。警告と旧パス対応表を画面および保存先の `package_warnings.json`・`package_mappings.json` に記録します。

保存先の `package_status.json` が `"ready": true` になってから、USB側の `起動.cmd` を実行します。USB側には別のファームIDと待機キューが作られます。結果・途中画像・作業領域もUSB側に保存します。ドライブ文字の変更に対応しますが、共有側との自動同期・結果返却はありません。移動前はワーカーを安全に停止し、Mayaの終了を確認してください。

## 8. プログラム構成とCLI/API

| ファイル | 主な役割 |
|---|---|
| [farm_cli.py](farm_cli.py) / [launch.ps1](launch.ps1) | CLI・GUI入口、Maya付属Pythonの選択 |
| [farm/gui.py](farm/gui.py) | 日本語ダッシュボードと画面上の操作・進捗 |
| [farm/store.py](farm/store.py) | 共有状態、キュー、所有トークン、排他、revision検査 |
| [farm/catalog.py](farm/catalog.py) / [farm/assets.py](farm/assets.py) | 最新シーン候補、一覧更新、調査、投入、USB素材収集 |
| [farm/maya_entry.py](farm/maya_entry.py) / [farm/images.py](farm/images.py) | Maya内での調査・レンダー、画像と再開記録の検証 |
| [farm/worker.py](farm/worker.py) / [farm/process.py](farm/process.py) | ジョブ取得、Maya監督、タイムアウト、成果物公開、子プロセス管理 |
| [farm/asset_paths.py](farm/asset_paths.py) | 旧素材パスの読み替えと未解決警告 |
| [farm/post.py](farm/post.py) / [farm/post_entry.py](farm/post_entry.py) / [farm/late_post.py](farm/late_post.py) | 投入時・完了後の動画化とデノイズ |
| [farm/results.py](farm/results.py) / [farm/results_dashboard.py](farm/results_dashboard.py) | 読みやすい結果フォルダ、旧結果移行、HTMLと連結動画 |
| [farm/package.py](farm/package.py) | 独立したUSBファームの作成 |

CLIは対応するMayaの `mayapy.exe` で実行します。`farm_cli.py` は省略時に自身の置かれたフォルダをルートにします。別ルートを操作する場合はサブコマンドより前に `--root <フォルダ>` を付けてください。

```powershell
$mayaPython = 'C:\Program Files\Autodesk\Maya2027\bin\mayapy.exe'
& $mayaPython -B .\farm_cli.py status
& $mayaPython -B .\farm_cli.py diagnose
& $mayaPython -B .\farm_cli.py add-project '.\projects\作品A' --maya 2027
& $mayaPython -B .\farm_cli.py scan
& $mayaPython -B .\farm_cli.py scan --force
& $mayaPython -B .\farm_cli.py enqueue <scene-id>
& $mayaPython -B .\farm_cli.py enqueue <scene-id> --maya-version 2027
& $mayaPython -B .\farm_cli.py worker
& $mayaPython -B .\farm_cli.py job <job-id> retry --revision <現在のrevision>
& $mayaPython -B .\farm_cli.py job-post <job-id> --revision <現在のrevision> --video on --fastdvdnet off
& $mayaPython -B .\farm_cli.py postprocess <完了job-id> --video
& $mayaPython -B .\farm_cli.py results-dashboard --fps 24
```

主なPython APIは `Store`、`refresh_scene_list`、`enqueue`、`Supervisor`、`export_package`、`run_late_postprocess`、`build_dashboard` です。CLIの `job` と `job-post` は現在のrevisionが必須です。`status` でID・revision・状態を読み、変更後は再読込してください。共有状態のschemaは1です。開発者は `mayapy.exe -B -m unittest discover -s tests -p 'test_*.py'` で独立した自動テストを実行できます。実Maya/複数PC/USBの実機試験は別途必要で、2026実機試験も未完了です。実施済みの範囲は [VALIDATION.md](VALIDATION.md) を参照してください。

## 9. 困ったときの確認先

| 現象 | 確認するもの |
|---|---|
| シーンが一覧にない・古い | 登録先の `scenes/` と各シーンフォルダ直下のファイル更新日時、「更新を確認」の進捗。必要なら「シーン情報を強制再取得」 |
| ジョブが待機したまま | 「PCの状態」のワーカー起動状態、ジョブ指定のMaya版・必要プラグイン、「環境診断」の結果。FastDVDnet/FFmpegを選択したジョブは対応する後処理環境も必要 |
| レンダー中に画像が増えない | ジョブの `work` が示す `workers/<PC名_ID>/jobs/<ジョブID>/maya_status.json`、`logs/`、`rendering/`。30分判定は完成画像一式の生成を対象にする |
| エラーで停止した | ジョブキューの「詳細」、`data/state.json` の `jobs[ID].error`、作業領域の `failure.json` と `logs/`。原因を解消後に「再試行」 |
| PCが落ちた・共有先が切れた | 元PCのMaya・ワーカーの生存状況と最後のハートビート。元PCの停止を確認せず他PCに回収しない |
| 結果を探したい | ジョブキューの「画像フォルダを開く」。ジョブの `publication` は公開先、`work` は途中画像・ログの保存先 |
| 動画だけ失敗した | `post_manifest.json`、作業領域の `processed/`、後処理ログ、環境診断。失敗ジョブなら後処理設定の変更後に再試行、完了済みなら追加後処理 |

トラブル調査でも、他PCが所有しているジョブのトークンや `data/state.json` を直接編集しないでください。古いGUIやワーカーのコードを使っている可能性がある場合は、それぞれ安全に停止して起動し直します。
