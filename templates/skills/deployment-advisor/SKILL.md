---
name: deployment-advisor
description: Deployment strategy guidance - platform selection, CI/CD pipeline design, environment configuration, monitoring
short_desc: deployment platform + CI/CD strategy, env config
keywords: ["deployment platform", "CI/CD pipeline", Vercel, "Fly.io", Railway, "monitoring setup", "deploy this", "how to deploy", "choose a platform for", Heroku, Render, "Cloudflare Workers"]
model: sonnet
---

# Deployment Advisor (Sonnet)

**Purpose**: Deployment strategy guidance - platform selection, CI/CD pipeline design, environment configuration, monitoring.

**Model**: Sonnet

## When to Invoke Autonomously

1. **Platform Selection**: User asks "Deploy to Vercel, AWS, GCP, or self-hosted?"
2. **CI/CD Design**: "Set up deployment pipeline for [project]"
3. **Environment Config**: "Manage secrets and environment variables"
4. **Monitoring Setup**: "Set up monitoring and logging for production"
5. **Pre-Production Review**: Before first production deployment
6. **Infrastructure Migration**: Moving between platforms or providers
7. **Scaling Strategy**: "How to scale deployment for [load requirements]"

## DO NOT invoke for

- Just deploying (follow existing process)
- Already-designed deployment with clear steps
- Simple static site deploys (obvious platform choice)

## What This Skill Does

**Platform Comparison**:
- Analyze app requirements (frontend/backend, serverless, containers)
- Compare platforms (Vercel, Netlify, AWS, GCP, self-hosted)
- Recommend based on cost, complexity, scalability
- Consider vendor lock-in vs convenience tradeoffs

**CI/CD Pipeline Design**:
- Design build → test → deploy workflow
- Select tools (GitHub Actions, GitLab CI, Jenkins)
- Configure automated testing gates
- Set up environment-specific deployments

**Environment Configuration**:
- Secrets management strategy (platform secrets, Vault, env files)
- Environment variable organization
- Multi-environment setup (dev, staging, prod)
- Security best practices (rotation, least privilege)

**Monitoring & Observability**:
- Uptime monitoring recommendations
- Error tracking integration
- Performance monitoring setup
- Log aggregation strategy
- Alert configuration (thresholds, escalation)

**See**: `examples/platform-comparison.md` and `examples/cicd-workflows.yml` for reference implementations, `templates/deployment-strategy.md` for output structure

## Quick Workflow Reference

**Before recommending**: Search for proven deployment patterns
```bash
.claude/scripts/kg-search search "deployment" --type concept
```

**For deep research**: run `hybrid_search("<platform comparison topic>")` (Weaviate MCP)

**Development env**: Python 3.12, Weaviate on :8081, Ollama on :11435; activate the project's own venv (`source .venv/bin/activate`) for project code.

## Success Metrics

- ✅ Deployment is automated and reliable (no manual steps)
- ✅ Secrets are managed securely (never in code or logs)
- ✅ Monitoring catches issues before users report them
- ✅ Cost is predictable and within budget
- ✅ Platform choice fits technical requirements and team skills
