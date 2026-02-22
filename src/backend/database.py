"""
MongoDB database configuration and setup for Mergington High School API
"""

import copy
import os
from typing import Any, Dict, Iterable, List, Optional

from pymongo import MongoClient
from argon2 import PasswordHasher, exceptions as argon2_exceptions


class _UpdateResult:
    def __init__(self, modified_count: int):
        self.modified_count = modified_count


class InMemoryCollection:
    def __init__(self):
        self._documents: Dict[str, Dict[str, Any]] = {}

    def _get_value(self, doc: Dict[str, Any], field: str) -> Any:
        current: Any = doc
        for part in field.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def _matches(self, doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
        for key, expected in query.items():
            value = self._get_value(doc, key)

            if isinstance(expected, dict):
                if "$in" in expected:
                    candidates = expected["$in"]
                    if isinstance(value, list):
                        if not any(item in candidates for item in value):
                            return False
                    elif value not in candidates:
                        return False
                if "$gte" in expected and (value is None or value < expected["$gte"]):
                    return False
                if "$lte" in expected and (value is None or value > expected["$lte"]):
                    return False
            else:
                if value != expected:
                    return False

        return True

    def count_documents(self, query: Dict[str, Any]) -> int:
        return sum(1 for _ in self.find(query))

    def insert_one(self, doc: Dict[str, Any]):
        document_id = doc.get("_id")
        if document_id is None:
            raise ValueError("Document must include _id")
        self._documents[document_id] = copy.deepcopy(doc)

    def find(self, query: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
        effective_query = query or {}
        for doc in self._documents.values():
            if self._matches(doc, effective_query):
                yield copy.deepcopy(doc)

    def find_one(self, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for doc in self.find(query):
            return doc
        return None

    def update_one(self, query: Dict[str, Any], update: Dict[str, Any]) -> _UpdateResult:
        for document_id, doc in self._documents.items():
            if not self._matches(doc, query):
                continue

            modified = False
            if "$push" in update:
                for key, value in update["$push"].items():
                    current = doc.setdefault(key, [])
                    if value not in current:
                        current.append(value)
                        modified = True

            if "$pull" in update:
                for key, value in update["$pull"].items():
                    current = doc.get(key, [])
                    if value in current:
                        doc[key] = [item for item in current if item != value]
                        modified = True

            self._documents[document_id] = doc
            return _UpdateResult(modified_count=1 if modified else 0)

        return _UpdateResult(modified_count=0)

    def aggregate(self, pipeline: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
        docs: List[Dict[str, Any]] = [copy.deepcopy(doc) for doc in self._documents.values()]

        for stage in pipeline:
            if "$unwind" in stage:
                field_path = stage["$unwind"].lstrip("$")
                unwound_docs: List[Dict[str, Any]] = []
                for doc in docs:
                    values = self._get_value(doc, field_path)
                    if isinstance(values, list):
                        for value in values:
                            new_doc = copy.deepcopy(doc)
                            parent = new_doc
                            parts = field_path.split(".")
                            for part in parts[:-1]:
                                parent = parent[part]
                            parent[parts[-1]] = value
                            unwound_docs.append(new_doc)
                docs = unwound_docs

            elif "$group" in stage:
                group_id = stage["$group"].get("_id")
                # If _id is a string, treat it as a field path and group by that field.
                if isinstance(group_id, str):
                    group_field = group_id.lstrip("$")
                    seen = set()
                    grouped_docs = []
                    for doc in docs:
                        value = self._get_value(doc, group_field)
                        if value not in seen:
                            seen.add(value)
                            grouped_docs.append({"_id": value})
                    docs = grouped_docs
                else:
                    # For non-string _id (e.g. constants or complex expressions),
                    # fall back to grouping all documents into a single bucket
                    # with that _id value. This avoids AttributeError from calling
                    # string methods on non-string values.
                    if docs:
                        docs = [{"_id": group_id}]
                    else:
                        docs = []

            elif "$sort" in stage:
                sort_field, direction = next(iter(stage["$sort"].items()))
                reverse = direction == -1
                docs = sorted(
                    docs,
                    key=lambda d: (d.get(sort_field) is None, d.get(sort_field)),
                    reverse=reverse,
                )

        for doc in docs:
            yield doc


def _create_collections():
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    timeout_ms = int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "2000"))

    try:
        mongo_client = MongoClient(mongodb_uri, serverSelectionTimeoutMS=timeout_ms)
        mongo_client.admin.command("ping")
        database = mongo_client["mergington_high"]
        return mongo_client, database["activities"], database["teachers"], "mongodb"
    except Exception:
        return None, InMemoryCollection(), InMemoryCollection(), "in-memory"


client, activities_collection, teachers_collection, DATABASE_BACKEND = _create_collections()

# Methods


def hash_password(password):
    """Hash password using Argon2"""
    ph = PasswordHasher()
    return ph.hash(password)


def verify_password(hashed_password: str, plain_password: str) -> bool:
    """Verify a plain password against an Argon2 hashed password.

    Returns True when the password matches, False otherwise.
    """
    ph = PasswordHasher()
    try:
        ph.verify(hashed_password, plain_password)
        return True
    except argon2_exceptions.VerifyMismatchError:
        return False
    except Exception:
        # For any other exception (e.g., invalid hash), treat as non-match
        return False


def init_database():
    """Initialize database if empty"""

    # Initialize activities if empty
    if activities_collection.count_documents({}) == 0:
        for name, details in initial_activities.items():
            activities_collection.insert_one({"_id": name, **details})

    # Initialize teacher accounts if empty
    if teachers_collection.count_documents({}) == 0:
        for teacher in initial_teachers:
            teachers_collection.insert_one(
                {"_id": teacher["username"], **teacher})


# Initial database if empty
initial_activities = {
    "Chess Club": {
        "description": "Learn strategies and compete in chess tournaments",
        "schedule": "Mondays and Fridays, 3:15 PM - 4:45 PM",
        "schedule_details": {
            "days": ["Monday", "Friday"],
            "start_time": "15:15",
            "end_time": "16:45"
        },
        "max_participants": 12,
        "participants": ["michael@mergington.edu", "daniel@mergington.edu"]
    },
    "Programming Class": {
        "description": "Learn programming fundamentals and build software projects",
        "schedule": "Tuesdays and Thursdays, 7:00 AM - 8:00 AM",
        "schedule_details": {
            "days": ["Tuesday", "Thursday"],
            "start_time": "07:00",
            "end_time": "08:00"
        },
        "max_participants": 20,
        "participants": ["emma@mergington.edu", "sophia@mergington.edu"]
    },
    "Morning Fitness": {
        "description": "Early morning physical training and exercises",
        "schedule": "Mondays, Wednesdays, Fridays, 6:30 AM - 7:45 AM",
        "schedule_details": {
            "days": ["Monday", "Wednesday", "Friday"],
            "start_time": "06:30",
            "end_time": "07:45"
        },
        "max_participants": 30,
        "participants": ["john@mergington.edu", "olivia@mergington.edu"]
    },
    "Soccer Team": {
        "description": "Join the school soccer team and compete in matches",
        "schedule": "Tuesdays and Thursdays, 3:30 PM - 5:30 PM",
        "schedule_details": {
            "days": ["Tuesday", "Thursday"],
            "start_time": "15:30",
            "end_time": "17:30"
        },
        "max_participants": 22,
        "participants": ["liam@mergington.edu", "noah@mergington.edu"]
    },
    "Basketball Team": {
        "description": "Practice and compete in basketball tournaments",
        "schedule": "Wednesdays and Fridays, 3:15 PM - 5:00 PM",
        "schedule_details": {
            "days": ["Wednesday", "Friday"],
            "start_time": "15:15",
            "end_time": "17:00"
        },
        "max_participants": 15,
        "participants": ["ava@mergington.edu", "mia@mergington.edu"]
    },
    "Art Club": {
        "description": "Explore various art techniques and create masterpieces",
        "schedule": "Thursdays, 3:15 PM - 5:00 PM",
        "schedule_details": {
            "days": ["Thursday"],
            "start_time": "15:15",
            "end_time": "17:00"
        },
        "max_participants": 15,
        "participants": ["amelia@mergington.edu", "harper@mergington.edu"]
    },
    "Drama Club": {
        "description": "Act, direct, and produce plays and performances",
        "schedule": "Mondays and Wednesdays, 3:30 PM - 5:30 PM",
        "schedule_details": {
            "days": ["Monday", "Wednesday"],
            "start_time": "15:30",
            "end_time": "17:30"
        },
        "max_participants": 20,
        "participants": ["ella@mergington.edu", "scarlett@mergington.edu"]
    },
    "Math Club": {
        "description": "Solve challenging problems and prepare for math competitions",
        "schedule": "Tuesdays, 7:15 AM - 8:00 AM",
        "schedule_details": {
            "days": ["Tuesday"],
            "start_time": "07:15",
            "end_time": "08:00"
        },
        "max_participants": 10,
        "participants": ["james@mergington.edu", "benjamin@mergington.edu"]
    },
    "Debate Team": {
        "description": "Develop public speaking and argumentation skills",
        "schedule": "Fridays, 3:30 PM - 5:30 PM",
        "schedule_details": {
            "days": ["Friday"],
            "start_time": "15:30",
            "end_time": "17:30"
        },
        "max_participants": 12,
        "participants": ["charlotte@mergington.edu", "amelia@mergington.edu"]
    },
    "Weekend Robotics Workshop": {
        "description": "Build and program robots in our state-of-the-art workshop",
        "schedule": "Saturdays, 10:00 AM - 2:00 PM",
        "schedule_details": {
            "days": ["Saturday"],
            "start_time": "10:00",
            "end_time": "14:00"
        },
        "max_participants": 15,
        "participants": ["ethan@mergington.edu", "oliver@mergington.edu"]
    },
    "Science Olympiad": {
        "description": "Weekend science competition preparation for regional and state events",
        "schedule": "Saturdays, 1:00 PM - 4:00 PM",
        "schedule_details": {
            "days": ["Saturday"],
            "start_time": "13:00",
            "end_time": "16:00"
        },
        "max_participants": 18,
        "participants": ["isabella@mergington.edu", "lucas@mergington.edu"]
    },
    "Sunday Chess Tournament": {
        "description": "Weekly tournament for serious chess players with rankings",
        "schedule": "Sundays, 2:00 PM - 5:00 PM",
        "schedule_details": {
            "days": ["Sunday"],
            "start_time": "14:00",
            "end_time": "17:00"
        },
        "max_participants": 16,
        "participants": ["william@mergington.edu", "jacob@mergington.edu"]
    }
}

initial_teachers = [
    {
        "username": "mrodriguez",
        "display_name": "Ms. Rodriguez",
        "password": hash_password("art123"),
        "role": "teacher"
    },
    {
        "username": "mchen",
        "display_name": "Mr. Chen",
        "password": hash_password("chess456"),
        "role": "teacher"
    },
    {
        "username": "principal",
        "display_name": "Principal Martinez",
        "password": hash_password("admin789"),
        "role": "admin"
    }
]
