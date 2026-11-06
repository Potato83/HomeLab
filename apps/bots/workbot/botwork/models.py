from dataclasses import asdict, dataclass, field
import re
import unicodedata


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).lower().replace("ё", "е").replace("‐", "-").replace("‑", "-").replace("–", "-").replace("—", "-")).strip()


@dataclass
class Vacancy:
    source: str
    external_id: str
    title: str
    company: str
    url: str
    description: str = ""
    categories: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    work_format: str = "unknown"
    level: str = "unknown"
    active: bool = True
    salary: str = "не указана"
    salary_min: int | float | None = None
    salary_max: int | float | None = None
    salary_currency: str = ""
    salary_period: str = "unknown"
    employment_types: list[str] = field(default_factory=list)
    employment_known: bool = False
    experience: str = ""
    work_format_label: str = ""
    notes: list[str] = field(default_factory=list)
    recruitment_status: str = "open"
    application_url: str = ""
    kind: str = "vacancy"
    start_at: str = ""
    end_at: str = ""
    registration_deadline: str = ""
    event_type: str = "unknown"
    event_status: str = "scheduled"
    language: str = "не указан"
    price: str = "не указана"
    price_kind: str = "unknown"
    language_codes: list[str] = field(default_factory=list)
    event_check_error: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class Decision:
    matched: bool
    reason: str
    warnings: list[str] = field(default_factory=list)
    matching_profiles: list[str] = field(default_factory=list)
