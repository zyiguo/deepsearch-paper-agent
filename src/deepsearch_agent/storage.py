import json
from pathlib import Path

from deepsearch_agent.qa import Answer


class JsonlAnswerStore:
    def __init__(self, path: Path):
        self.path = path

    def append(self, question: str, answer: Answer) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "question": question,
            "answer": {
                "text": answer.text,
                "sources": [
                    {
                        "source_path": source.source_path,
                        "page_number": source.page_number,
                        "text": source.text,
                    }
                    for source in answer.sources
                ],
            },
        }

        with self.path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(record, ensure_ascii=False)
                + "\n"
            )

    def list_records(self) -> list[dict]:
        if not self.path.exists():
            return []

        records = []

        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    records.append(json.loads(line))

        return records