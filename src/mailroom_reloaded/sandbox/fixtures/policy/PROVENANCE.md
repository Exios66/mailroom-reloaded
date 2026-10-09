# Vendored policy files

Verbatim copies of files from `Exios66/mailroom-sandbox-content` v0.5.0
(`f650cfd`), so the sandbox server runs the smoke set with the same ingress,
recipient, send-schedule and delegation policy as the full pack, with zero
network. When the server is pointed at a full content directory it reads the
originals from there (`email/*.yaml`, `protocol/delegation_matrix.csv`) and
these copies are not used.

| File | Source path | sha256 |
|---|---|---|
| `ingress_policy.yaml` | `email/ingress_policy.yaml` | `33877b489a2ff250ede47deae14605d1baad7077fb8465d1d60047238534e65a` |
| `recipient_policy.yaml` | `email/recipient_policy.yaml` | `eb07a8305eb3517c188829489502d18ac5e69d8f2870a022edcfb18b36bedccf` |
| `send_schedule.yaml` | `email/send_schedule.yaml` | `4d9c1d9a7927ea01dc25895fb1cbc0e9909998de41a55b1f3335d8aa165be816` |
| `delegation_matrix.csv` | `protocol/delegation_matrix.csv` | `8416ee35d6bb2b33b3616dd577d5830dd4ef410a24a64b80afadc71c9aa3d8fa` |

Do not edit these files; change the content repo and re-copy. They live outside
`fixtures/smoke/` because `mailroom sandbox content build` regenerates that
directory wholesale.
