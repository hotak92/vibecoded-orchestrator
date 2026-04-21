# Bad Use Cases - When NOT to Use AI Model Selector

## Scenario 1: Model Already Decided

**User Request**: "Already using GPT-4 via OpenAI API, implement chat feature"

**Why NOT to Use**:
- ❌ Model already chosen (GPT-4)
- ❌ Using API (not self-hosted selection)
- ❌ Task is implementation, not model selection

**What to Do Instead**: Proceed with implementation using specified model

---

## Scenario 2: Prototyping (Model Choice Not Critical)

**User Request**: "Just prototyping, any model works, show me quick demo"

**Why NOT to Use**:
- ❌ User explicitly says model doesn't matter
- ❌ Focus on rapid prototyping, not optimization
- ❌ Premature optimization (decide model later)

**What to Do Instead**: Pick any reasonable default (Llama-3-8B, Qwen2.5-7B) and continue

---

## Scenario 3: Deploying Existing Model

**User Request**: "Deploy existing Qwen2.5-7B model to production"

**Why NOT to Use**:
- ❌ Model already selected and tested
- ❌ Task is deployment, not selection
- ❌ No decision point

**What to Do Instead**: Focus on deployment strategy (use /deployment-advisor if needed)

---

## Scenario 4: Non-Self-Hosted Models

**User Request**: "Should I use Claude or GPT-4 for my app?"

**Why NOT to Use**:
- ❌ API models (not self-hosted)
- ❌ Different decision criteria (pricing, features, not VRAM)
- ❌ Skill focuses on self-hosted models (Ollama, local LLMs)

**What to Do Instead**: Discuss API model tradeoffs directly (cost, latency, features)

---

## Scenario 5: Model Already Performing Well

**User Request**: "Current model (Mistral-7B) works great, should I explore alternatives?"

**Why NOT to Use**:
- ❌ If it ain't broke, don't fix it
- ❌ No specific problem to solve (quality, speed, VRAM)
- ❌ Optimization without clear need

**What to Do Instead**: "Current model meets requirements. Explore alternatives only if you have specific needs (faster, better quality, lower VRAM)"

---

## Scenario 6: Trivial Tasks (Model Overkill)

**User Request**: "Need model to classify text into 3 categories (positive/negative/neutral)"

**Why NOT to Use** (potentially):
- ❌ Simple classification might not need LLM
- ❌ Could use lightweight classifier (BERT, distilBERT)
- ❌ LLM is overkill for this task

**What to Do Instead**: Suggest lightweight alternative first, use LLM only if needed for complex cases

---

## Scenario 7: Hardware Unknown

**User Request**: "Which model should I use? I don't know my VRAM"

**Why NOT to Use** (yet):
- ❌ Missing critical constraint (VRAM)
- ❌ Can't make recommendation without knowing hardware

**What to Do Instead**: Ask for hardware specs first:
```
"To recommend a model, I need to know:
- GPU: [Model and VRAM] or CPU-only?
- RAM: [How much system RAM]
- Task: [What are you building]

Please provide these details."
```

---

## Scenario 8: Enterprise/Managed Solutions

**User Request**: "Choosing between Azure OpenAI, AWS Bedrock, GCP Vertex AI"

**Why NOT to Use**:
- ❌ Managed cloud services, not self-hosted
- ❌ Different selection criteria (pricing, SLA, integration)
- ❌ Skill is for Ollama/local models

**What to Do Instead**: Discuss cloud service tradeoffs (pricing models, vendor lock-in, features)

---

## Scenario 9: Fine-Tuning Question

**User Request**: "Should I fine-tune Llama-3-8B or use a different base model?"

**Why NOT to Use** (primarily):
- ❌ Question is about fine-tuning strategy, not base model selection
- ❌ Different concern (adaptation vs initial selection)

**What to Do Instead**: Discuss fine-tuning approach first. If base model selection becomes relevant, then use skill.

---

## Scenario 10: Non-AI Task

**User Request**: "Should I use PostgreSQL or MongoDB for my database?"

**Why NOT to Use**:
- ❌ Not an AI model selection question
- ❌ Database choice (use /database-advisor skill)

**What to Do Instead**: Use appropriate skill for the domain (/database-advisor)

---

## Red Flags - Don't Use This Skill If:

1. **Model already chosen**: "Already using X model"
2. **API models**: "GPT-4", "Claude", "Gemini" (unless asking about self-hosted alternatives)
3. **Prototyping**: "Just need something quick", "Model doesn't matter"
4. **No VRAM constraint**: Missing critical information for recommendation
5. **Non-model question**: About deployment, fine-tuning, serving, not model choice
6. **Task too simple**: Simple classification, regex would work, rule-based sufficient
7. **Enterprise solutions**: Azure OpenAI, AWS Bedrock, GCP Vertex (managed services)
8. **Already optimal**: Current model performs well, no issues to solve

---

## Decision Rule

**Use this skill** when:
- ✅ User needs to SELECT a self-hosted model
- ✅ VRAM or hardware constraints are known (or can be asked)
- ✅ Task is defined (code gen, chat, vision, embeddings)
- ✅ Choice impacts project significantly (not prototyping)

**Don't use this skill** when:
- ❌ Model already decided
- ❌ Using API/managed services
- ❌ Prototyping (model quality not critical yet)
- ❌ Question is about deployment, not selection
- ❌ Task doesn't need LLM (overkill)
