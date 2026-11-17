"""Verify the HA JSON contract does not turn unknown totals into zero/active energy."""
import json
import math
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor('!secret', lambda loader, node: loader.construct_scalar(node))


@pytest.mark.parametrize('extra,expected', [
    ({'calories': 100.5}, {'active_calories': 100}),
    ({'calories': 1850.72, 'calories_type': 'total'}, {'total_calories': 1850.72}),
    ({'calories': None, 'calories_type': 'total'}, {}),
    ({'calories': 'unavailable', 'calories_type': 'total'}, {}),
    ({'calories_type': 'total'}, {}),
    ({'calories': 0, 'calories_type': 'total'}, {'total_calories': 0}),
])
def test_rest_command_energy_type(extra, expected):
    path = Path(__file__).resolve().parents[1] / 'homeassistant' / 'rest_command.yaml'
    payload = yaml.load(path.read_text(encoding='utf-8'), Loader=Loader)['rest_command']['send_activity_to_life_os']['payload']
    def is_number(value):
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False
    env = Environment()
    env.globals['is_number'] = is_number
    env.filters['to_json'] = json.dumps
    result = json.loads(env.from_string(payload).render(profile='admin', steps=6202, **extra))
    assert result == {'profile_alias': 'admin', 'steps': 6202, **expected}
