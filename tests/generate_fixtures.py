import json
import os
import random
from tests.mock_logs import generate_mock_log
from devops_agent.api.models import RootCauseCategory


def generate_fixtures(num_samples: int = 50):
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
    os.makedirs(fixtures_dir, exist_ok=True)

    categories = list(RootCauseCategory)

    for i in range(num_samples):
        # Pick a random category
        category = random.choice(categories)

        # Get the mock log text
        log_text = generate_mock_log(category.value)

        # We can add some slight variation to the text if desired, but this is enough
        fixture_data = {"expected_category": category.value, "log": log_text}

        file_path = os.path.join(fixtures_dir, f"sample_{i:02d}.json")
        with open(file_path, "w") as f:
            json.dump(fixture_data, f, indent=2)

    print(f"Generated {num_samples} mock logs in {fixtures_dir}")


if __name__ == "__main__":
    generate_fixtures()
