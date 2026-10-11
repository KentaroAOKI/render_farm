# 開発・GitHub公開手順

## 公開対象

ソースコード、起動スクリプト、テストコード、ドキュメントを管理します。
キューや素材キャッシュを含む `data/`、PC固有設定を含む `workers/`、
`results/`、`staging/`、`projects/`、`examples/`、`tests/runtime/`、
USB梱包状態の `package_status.json` は `.gitignore` で除外します。
ルートと `farm/`・`tests/`・`tools/` には公開ファイルの許可リストも設けています。
新しいソース・文書を加えた場合は、内容を確認してから `.gitignore` の許可項目に追加してください。
内部シーンに固定した手動実機スクリプトと詳細な運用記録は公開対象から除外します。

USBパッケージや任意の場所にコピーした実プロジェクトはリポジトリの外へ保存してください。
独自の素材フォルダをルート内に置く場合は、ステージング前に除外規則を追加してください。
`.gitignore` はすでに追跡されているファイルには適用されません。

## 初回公開の準備

ツールのルートでPowerShellを開き、次を実行します。
現在の共有フォルダはGit初期化済みです。`.git` がない新しいコピーで始める場合だけ `git init -b main` を先に実行します。

```powershell
git add --dry-run .
git add .
git status --short
git diff --cached --stat
git diff --cached
```

差分に実シーン、素材、画像、ログ、PC名・個人パス、認証情報がないことを確認します。
公開コードのライセンスはまだ設定していません。採用するライセンスを決めたら
`LICENSE` を追加してください。Maya・Arnold・外部ツール・学習済みモデルの配布物は含めません。

確認後にコミットし、GitHubに作成した空のリポジトリのURLを指定します。

```powershell
git commit -m "Initial portable Maya render farm"
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

`OWNER/REPOSITORY` は実際の公開先に置き換えます。
共有UNCフォルダでGitが所有者の違いを警告した場合は、信頼できる保存先だと確認した上で、各コマンドに `-c safe.directory=<そのリポジトリの絶対パス>` を付けて実行できます。共有先を一括して信頼する設定は避けてください。

## 開発と検証

Windowsと対象バージョンのMaya付属Pythonを使います。GUIはMaya付属のPySide6を使用します。
コードの改行はLF、Windows起動スクリプトはCRLF、文字コードはUTF-8です。
Windows PowerShell 5.1で実行する `.ps1` に非ASCII文字を追加する場合は、
読み取り時の文字化けを避けるため、UTF-8 BOMの扱いも確認してください。

```powershell
& 'C:\Program Files\Autodesk\Maya2027\bin\mayapy.exe' -B -m unittest discover -s tests -p 'test_*.py' -v
```

2025・2026を検証する場合は実行ファイルのバージョンを変更します。
テスト生成物は `tests/runtime/` に保存されます。実機レンダーの結果と未検証項目は
`VALIDATION.md` に記録してください。模擬Mayaの試験と実機試験を区別します。
GUI・GPU・ライセンスを必要とする実機試験は、対応する環境で実行してください。

変更時は共有フォルダでの排他制御、所有トークン、途中再開、ルート内の作業領域を維持します。
既存の別ファームやユーザーが起動したMayaを停止・変更しないでください。
