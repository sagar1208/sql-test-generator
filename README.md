tmnl-tina-pipeline-bi-agents/
├── src/
│   ├── agents/
│   │      │   │
│   │   ├── sql_test_agent/
│   │   │   ├── sql_test_assistant.py
│   │   │   ├── agent.yaml
│   │   │   ├── Dockerfile
│   │   │   ├── requirements.txt
│   │   │   ├── README.md
│   │   │   └── tmnl-tina-glue-job-if-finance-fctksb1.py
│   │   │
│   │   └── agents_base/
│   │       └── tina_agent_base/
│   │           ├── __init__.py
│   │           ├── config.py
│   │           ├── runtime.py
│   │           └── session.py
│   │
│   └── lambdas/
│       └── agent_invoker/
│           └── handler.py
│
├── tf_modules/
│   ├── agent/
│   └── inference_profile/
│
├── tf_roots/
│   ├── anomaly_detector_agent/
│   ├── sql_generator_agent/
│   ├── sql_test_agent/
│   └── shared/
│
├── scripts/
│   ├── validate_manifests.py
│   ├── check_generated.py
│   └── new_agent.py
│
├── docs/
│   ├── agent-platform-brief.md
│   └── runbook.md
│
├── pyproject.toml
├── README.md
└── CONTRIBUTING.md
 
agent.yaml

    -> Terraform runtime configuration
 
Dockerfile

    -> ARM64 container image
 
requirements.txt

    -> Python dependencies
 
sql_test_assistant.py

    -> Strands + Bedrock generation

    -> card validation

    -> fallback generation

    -> AgentCore Memory

    -> AgentCore entrypoint
 
agent_invoker/handler.py

    -> Lambda front door

    -> session ID handling

    -> AgentCore runtime invocation
 
tf_roots/sql_test_agent/

    -> deploys the SQL test runtime
 
