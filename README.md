# LinuxMate — Complete Final Review Project

## Features
- Flask web application with a responsive dashboard.
- SQLite database automatically populated from the included 300-row CSV dataset.
- Local lexical retrieval from dataset records.
- Optional Ollama integration (`llama3.2:latest` by default); app falls back to local retrieval if Ollama is not running.
- Pattern-based warnings for selected risky commands.
- Question history, feedback storage, statistics, and health endpoint.
- 300 dataset records in CSV and JSON formats.

## Run on Windows (fastest)
1. Install Python 3.10+ and ensure `py` works in Command Prompt.
2. Extract the ZIP file. Open the extracted `LinuxMate_Complete` folder.
3. Double-click `run_windows.bat`. It installs Flask and starts the app.
4. Open http://127.0.0.1:5000 in your browser.

If PowerShell policy allows, `setup_and_run.ps1` creates a virtual environment.

## Optional Ollama model
1. Install Ollama from https://ollama.com/ and open it.
2. In a separate terminal run `ollama pull llama3.2:latest`. This download may take time and is optional.
3. Start/restart the app. The checkbox “Use Ollama model if available” will try Ollama; if unavailable, the app automatically uses local dataset retrieval.

## Verify dataset and database
After starting the app, open http://127.0.0.1:5000/health. Expected response includes `"dataset_records": 300`. SQLite database `linuxmate.db` is created in this folder. CSV is the source dataset.

## Project architecture
Browser UI → Flask routes → retrieval over SQLite Q&A dataset → optional Ollama generation → safety pattern scan → response and warning → history/feedback saved to SQLite.

## Important academic accuracy
This package implements retrieval-based prompting and an optional pretrained Ollama model. It does **not** fine-tune model weights. The 300 records are structured educational examples created for this prototype; they are not an independently validated benchmark. Report evaluation scores only after running a documented test set and recording actual outcomes.

## Suggested 3-hour build and demo plan
- 0:00–0:30: extract and run; verify `/health`.
- 0:30–1:00: test command questions and OS-concept questions; inspect CSV.
- 1:00–1:30: demonstrate safety warnings, history, and feedback.
- 1:30–2:00: run the test checklist and record observed outcomes.
- 2:00–3:00: prepare screenshots, architecture explanation, limitations, and viva answers.

## Suggested evaluation checklist
Test at least 20 questions, recording expected topic, observed topic, whether the answer is useful, whether safety warning appears for risky input, and fallback behavior with Ollama disabled. Do not claim a score until the tests have actually been run.
