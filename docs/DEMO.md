# Demo with fictional data

The following example runs an offline preflight without World of Warcraft or a provider account. The route, quest and character are invented. Run it in a Python 3.12 shell from the repository root.

```python
from datetime import UTC, datetime

from questguard.engine import audit_route, build_route
from questguard.ingest import Snapshot, parse_guide

pickup = parse_guide("""#group Demo
#name 40-41 Harbor
#next 41-42 Headland
step
.accept 990001 >> Accept A Signal at Dusk
""", metadata={"guideId": 1000})
objective = parse_guide("""#group Demo
#name 41-42 Headland
step
.complete 990001,1 >> Light the signal
""", metadata={"guideId": 2000})
route = build_route((pickup, objective), current_id=objective.id)
snapshot = Snapshot(
    "Demo character", current_guide=objective.name, current_group=objective.group,
    current_step=1, current_step_id=objective.steps[0].step_id,
    active=frozenset(), completed=frozenset(),
    exported_at=datetime.now(UTC),
)
print(audit_route(route, snapshot).render())
```

The result shows one confirmed missing pickup. The character has reached the objective in **41–42 Headland**, and the earlier acceptance step is in **40–41 Harbor**.

```text
[WARNING] Step 1, A Signal at Dusk (990001)
  Pickup in 40-41 Harbor, step 1.
```

Set `active=frozenset({990001})` and run the example again. The quest appears as active and the missing-pickup warning disappears.
