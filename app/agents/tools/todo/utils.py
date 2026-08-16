from pathlib import Path
from app.config.settings import settings

def todo_file(session_id : str)->Path:
    root = Path(settings.runtime_data_dir) / "todos" / session_id
    root.mkdir(parents=True, exist_ok=True)
    return root/"todo.json"