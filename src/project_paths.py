from pathlib import Path


def project_root() -> Path:
    here = Path(__file__).resolve().parent
    if here.name == "src":
        return here.parent
    return here


def outputs_dir(*parts: str) -> Path:
    path = project_root().joinpath("outputs", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
