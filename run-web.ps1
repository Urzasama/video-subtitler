# start the local web UI
$py = if (Test-Path \".venv\Scripts\python.exe\") { \".venv\Scripts\python.exe\" } else { \"python\" }
& $py server.py
