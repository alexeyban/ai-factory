# Phase 7 — Benchmarking Pipeline

**Приоритет:** Medium
**Зависимости:** Phase 5 (Learning Loop)
**Блокирует:** ничего

---

## Цель

Создать training environment с измеримыми метриками прогресса: датасет задач разной сложности, curriculum learning (постепенное усложнение), Grafana dashboard для визуализации прогресса.

---

## Шаги реализации

### Шаг 1 — Dataset Format

**Файл для создания:** `benchmarks/datasets/easy.json`

Формат задачи в датасете:
```json
{
  "dataset_version": "1.0",
  "difficulty": "easy",
  "tasks": [
    {
      "task_id": "bench_easy_001",
      "title": "Reverse a string",
      "description": "Write a Python function reverse_string(s: str) -> str that returns the input string reversed.",
      "difficulty": "easy",
      "type": "dev",
      "tests": [
        "assert reverse_string('hello') == 'olleh'",
        "assert reverse_string('') == ''",
        "assert reverse_string('a') == 'a'"
      ],
      "expected_output": {
        "function_name": "reverse_string",
        "signature": "def reverse_string(s: str) -> str"
      },
      "hidden_tests": [
        "assert reverse_string('racecar') == 'racecar'",
        "assert reverse_string('Python') == 'nohtyP'"
      ],
      "time_limit_ms": 100,
      "memory_limit_mb": 50
    }
  ]
}
```

**Создать датасеты:**
- `benchmarks/datasets/easy.json` — 10+ задач (строки, списки, базовая математика)
- `benchmarks/datasets/medium.json` — 10+ задач (алгоритмы сортировки, деревья, DP)
- `benchmarks/datasets/hard.json` — 5+ задач (сложные алгоритмы, оптимизация)
- `benchmarks/datasets/expert.json` — 5+ задач (системное программирование, concurrency)

---

### Шаг 2 — Dataset Loader

**Файл для создания:** `benchmarks/dataset_loader.py`

```python
from pathlib import Path
import json
from dataclasses import dataclass

@dataclass
class BenchmarkTask:
    task_id: str
    title: str
    description: str
    difficulty: str      # easy | medium | hard | expert
    type: str
    tests: list[str]
    hidden_tests: list[str]
    expected_output: dict
    time_limit_ms: float = 1000.0
    memory_limit_mb: float = 100.0

    def to_task_contract(self) -> dict:
        """Конвертировать в TaskContract для передачи в workflow."""
        ...

class DatasetLoader:
    DIFFICULTIES = ['easy', 'medium', 'hard', 'expert']
    DATASETS_DIR = Path(__file__).parent / 'datasets'

    def load(self, difficulty: str) -> list[BenchmarkTask]:
        """Загружает все задачи указанной сложности."""
        path = self.DATASETS_DIR / f"{difficulty}.json"
        if not path.exists():
            raise FileNotFoundError(f"Dataset not found: {path}")
        data = json.loads(path.read_text())
        return [BenchmarkTask(**t) for t in data['tasks']]

    def load_all(self) -> dict[str, list[BenchmarkTask]]:
        """Загружает все датасеты."""
        return {d: self.load(d) for d in self.DIFFICULTIES if
                (self.DATASETS_DIR / f"{d}.json").exists()}

    def sample(self, difficulty: str, n: int = 1) -> list[BenchmarkTask]:
        """Случайная выборка N задач указанной сложности."""
        import random
        tasks = self.load(difficulty)
        return random.sample(tasks, min(n, len(tasks)))
```

---

### Шаг 3 — Curriculum Learning

**Файл для создания:** `benchmarks/curriculum.py`

```python
@dataclass
class CurriculumState:
    current_level: str = 'easy'
    level_stats: dict = field(default_factory=lambda: {
        'easy': {'attempts': 0, 'successes': 0},
        'medium': {'attempts': 0, 'successes': 0},
        'hard': {'attempts': 0, 'successes': 0},
        'expert': {'attempts': 0, 'successes': 0},
    })
    state_path: str = 'workspace/.ai_factory/curriculum_state.json'

class Curriculum:
    LEVELS = ['easy', 'medium', 'hard', 'expert']
    PROMOTION_THRESHOLD = 0.8  # success_rate для перехода на следующий уровень
    MIN_ATTEMPTS = 5           # минимум попыток перед оценкой уровня

    def __init__(self, loader: DatasetLoader, state: CurriculumState | None = None):
        self.loader = loader
        self.state = state or self._load_state()

    def get_next_task(self) -> BenchmarkTask:
        """
        Алгоритм:
        1. Определить текущий уровень из state
        2. Проверить можно ли продвинуться (success_rate >= threshold и attempts >= min)
        3. Если да — перейти на следующий уровень
        4. Вернуть случайную задачу текущего уровня
        """
        current = self.state.current_level
        if self._should_promote(current):
            next_level = self._next_level(current)
            if next_level:
                self.state.current_level = next_level
                self._save_state()
                current = next_level
        return self.loader.sample(current, n=1)[0]

    def record_result(self, task: BenchmarkTask, success: bool) -> None:
        """Обновить статистику после выполнения задачи."""
        stats = self.state.level_stats[task.difficulty]
        stats['attempts'] += 1
        if success:
            stats['successes'] += 1
        self._save_state()

    def get_success_rate(self, level: str) -> float:
        stats = self.state.level_stats[level]
        if stats['attempts'] == 0:
            return 0.0
        return stats['successes'] / stats['attempts']

    def _should_promote(self, level: str) -> bool:
        stats = self.state.level_stats[level]
        if stats['attempts'] < self.MIN_ATTEMPTS:
            return False
        return self.get_success_rate(level) >= self.PROMOTION_THRESHOLD

    def _next_level(self, level: str) -> str | None:
        idx = self.LEVELS.index(level)
        return self.LEVELS[idx + 1] if idx + 1 < len(self.LEVELS) else None

    def _load_state(self) -> CurriculumState: ...
    def _save_state(self) -> None: ...
```

---

### Шаг 4 — Prometheus + Grafana

**Файл для изменения:** `docker-compose.yml`

Добавить сервисы:
```yaml
prometheus:
  image: prom/prometheus:latest
  ports:
    - "9090:9090"
  volumes:
    - ./infra/prometheus.yml:/etc/prometheus/prometheus.yml
    - prometheus_data:/prometheus

grafana:
  image: grafana/grafana:latest
  ports:
    - "3000:3000"
  environment:
    GF_SECURITY_ADMIN_PASSWORD: admin
  volumes:
    - grafana_data:/var/lib/grafana
    - ./infra/grafana/dashboards:/etc/grafana/provisioning/dashboards
    - ./infra/grafana/datasources:/etc/grafana/provisioning/datasources
  depends_on:
    - prometheus
```

**Файл для создания:** `infra/prometheus.yml`
```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'ai-factory'
    static_configs:
      - targets: ['memory-worker:8080']
```

---

### Шаг 5 — Metrics Exporter

**Файл для создания:** `benchmarks/metrics_exporter.py`

```python
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Метрики
TASK_ATTEMPTS = Counter('ai_factory_task_attempts_total',
                         'Total task attempts', ['difficulty'])
TASK_SUCCESSES = Counter('ai_factory_task_successes_total',
                          'Successful tasks', ['difficulty'])
AVG_REWARD = Gauge('ai_factory_avg_reward', 'Average reward score', ['difficulty'])
SKILL_COUNT = Gauge('ai_factory_skill_count', 'Total active skills')
EXPLORATION_RATE = Gauge('ai_factory_exploration_rate', 'Current exploration rate')
EPISODE_COUNT = Counter('ai_factory_episodes_total', 'Total episodes completed')

class MetricsExporter:
    def __init__(self, port: int = 8080):
        self._port = port

    def start(self) -> None:
        start_http_server(self._port)

    def record_task_result(self, difficulty: str, success: bool, reward: float) -> None:
        TASK_ATTEMPTS.labels(difficulty=difficulty).inc()
        if success:
            TASK_SUCCESSES.labels(difficulty=difficulty).inc()
        AVG_REWARD.labels(difficulty=difficulty).set(reward)

    def update_skill_count(self, count: int) -> None:
        SKILL_COUNT.set(count)

    def update_exploration_rate(self, rate: float) -> None:
        EXPLORATION_RATE.set(rate)
```

---

### Шаг 6 — Grafana Dashboard

**Файл для создания:** `infra/grafana/dashboards/ai_factory.json`

Панели дашборда:
1. **Success Rate by Difficulty** — линейный график success_rate по уровням сложности
2. **Average Reward Trend** — avg_reward по эпизодам (должен расти)
3. **Skill Count Growth** — рост базы навыков
4. **Exploration Rate** — снижение exploration_rate со временем
5. **Episode Throughput** — количество эпизодов в час

---

### Шаг 7 — Тесты

**Файлы для создания:**
- `tests/test_dataset_loader.py`
- `tests/test_curriculum.py`

**`tests/test_dataset_loader.py`:**
- `test_load_easy_dataset()` — датасет загружается, содержит BenchmarkTask
- `test_dataset_has_required_fields()` — все обязательные поля присутствуют
- `test_sample_returns_n()` — выборка n задач работает
- `test_to_task_contract()` — конвертация в TaskContract корректна

**`tests/test_curriculum.py`:**
- `test_starts_at_easy()` — начальный уровень = easy
- `test_no_promotion_below_threshold()` — success_rate < 0.8 → не продвигаться
- `test_promotion_on_threshold()` — success_rate >= 0.8 и attempts >= min → продвижение
- `test_no_promotion_beyond_expert()` — с expert нет следующего уровня
- `test_state_persistence()` — состояние сохраняется и загружается

---

## Порядок выполнения

1. Создать `benchmarks/datasets/easy.json` (10+ задач)
2. Создать `benchmarks/dataset_loader.py`
3. Создать `benchmarks/curriculum.py`
4. Добавить Prometheus и Grafana в `docker-compose.yml`
5. Создать `infra/prometheus.yml`
6. Создать `benchmarks/metrics_exporter.py`
7. Создать базовый Grafana dashboard JSON
8. Написать тесты
9. `pytest tests/ -x`
10. Коммит: `feat(phase7): benchmarking pipeline and metrics dashboard`

---

## Критерии готовности

- [ ] `DatasetLoader.load('easy')` возвращает список задач
- [ ] `Curriculum.get_next_task()` возвращает задачи по уровням
- [ ] Продвижение по уровням при success_rate >= 0.8
- [ ] `docker compose up prometheus grafana` — сервисы поднимаются
- [ ] Grafana dashboard отображает метрики
- [ ] `pytest tests/test_dataset_loader.py` — зелёный
- [ ] `pytest tests/test_curriculum.py` — зелёный

---

## Риски

- **Hidden tests leakage:** hidden_tests не должны попадать в LLM промпт. Разделить test evaluation: публичные тесты для Dev агента, hidden для финальной оценки.
- **Dataset quality:** синтетические задачи могут не отражать реальную сложность. Рекомендуется использовать подмножество HumanEval или MBPP как основу.
- **Prometheus port conflict:** порт 9090 может быть занят. Сделать configurable через .env.
