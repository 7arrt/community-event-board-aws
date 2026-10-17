# Inception: Community Event Board (poc scope)

Team: Artur Tovmasyan (sole author)
Status: inception draft. Items marked **(assumption)** are unconfirmed.
IaC, an automated pipeline, monitoring and cleanup are in scope, even at poc scope.

## 1. Problem and goal

A city of ~50,000 residents has no single place where organizers publish events and residents browse and RSVP. Traffic is bursty: quiet on weekdays, peaking Friday to Sunday and on holidays.

**Goal of the prototype:** prove that the infrastructure can serve this workload shape (public, read-heavy, bursty, with image uploads and deferred email) using the simplest AWS building blocks, deployed and reachable by a public URL. The app code is a stub; the infrastructure decisions are the deliverable.

## 2. Users and core flows

| User | Flow | Prototype support |
|---|---|---|
| Resident | Browse events, view images | Real (stub app + CloudFront) |
| Resident | RSVP to an event, capacity enforced | Real (atomic counter in DB) |
| Organizer | Create/edit event with image (<= 5 MB) | Real (presigned upload to S3) |
| Resident | Receive reminder email | Design only |
| Admin | Dashboard | Out of scope |

## 3. Non-functional requirements

| Area | Target | Basis |
|---|---|---|
| Peak load | 500 concurrent users, ~100 req/s **(assumption: 1 request per 5 s per user)** | Scenario text + estimate |
| Read/write ratio | ~95% reads **(assumption)** | Event browsing dominates |
| Image size | <= 5 MB, JPEG/PNG only | Scenario text |
| Availability | Best effort for poc; single-AZ app tier accepted and documented | poc scope |
| Durability | Event data and images must not be lost on instance replacement | Design rule: no state on EC2 |
| Cost | Under ~$20 for the week **(assumption, confirm)**, resources removed after demo | Sandbox account |
| Security | Least-privilege IAM, no public buckets, no keys in repo or pipeline files (credentials only as pipeline secrets/variables on either provider) | Project baseline |
| HTTPS | CloudFront default certificate to users; CloudFront to ALB over HTTP, restricted to CloudFront only; no custom domain (trade-off to explain) | Design decision |
| Delivery | All infrastructure as code (CloudFormation). `.gitlab-ci.yml` and `.github/workflows/deploy.yml` deploy automatically on every push to `main` (preflight, validate, template lint, deploy, health check) and must exit 0. Basic monitoring (CloudWatch logs + one alarm). Cleanup deletes all resources | Project brief |

## 4. Architecture

```
Resident --> CloudFront --+--> ALB --> EC2 (stub app) --> DynamoDB (events, RSVPs)
                          |                 |
                          |                 +--> presigned URL --> S3 (images)
                          +--> S3 (images, via Origin Access Control)

Design only: EventBridge Scheduler --> SQS --> Lambda --> SES (reminders)
```

Placement: ALB and EC2 inside a VPC (single-AZ app tier accepted for poc).

**Request flows**
- Browse: browser -> CloudFront (cached) -> ALB -> EC2 -> DynamoDB. Images: CloudFront -> S3 directly.
- RSVP: EC2 does a conditional DynamoDB update (`count < capacity`), so two users cannot take the last seat.
- Upload: app issues a presigned S3 URL, the browser uploads straight to S3, so 5 MB files never touch the EC2 instance.

## 5. Key decisions (with alternatives)

| Decision | Choice | Alternatives considered | Why | What would change it |
|---|---|---|---|---|
| Compute | EC2 behind ALB | Lambda, ECS/Fargate | Fastest to build and explain for a poc; predictable for a steady stub app | Real app with team container skills -> Fargate; very spiky, stateless -> Lambda |
| Event/RSVP data | DynamoDB on-demand | RDS, SQLite on EC2 | No provisioning time, scales with bursts, atomic conditional updates fit RSVP capacity; SQLite would put state on one instance | Complex reporting/joins for the admin dashboard -> RDS |
| Images | S3 + presigned upload | Upload through app, EBS/EFS | Durable, cheap, keeps large files off the app tier | None at this scale |
| Delivery | CloudFront in front | ALB only | Weekend spikes are mostly reads; caching protects the origin and cuts latency | Fully dynamic, uncacheable traffic |
| Reminders | SQS + Lambda + SES (design only) | Send email from the app | Decouples slow email from user requests; absorbs bursts; retries | Tiny volume -> direct SES call |
| Provisioning | CloudFormation deployed by the CI/CD pipeline | Terraform, console/CLI | Repo requires IaC and automated deploy; CloudFormation needs no remote state backend (Terraform does) | Multi-cloud or reusable modules -> Terraform |

## 6. Security baseline

- ALB security group accepts traffic only from CloudFront (managed prefix list); EC2 security group accepts traffic only from the ALB security group.
- EC2 IAM role: read/write on one DynamoDB table and one S3 prefix only.
- S3: Block Public Access on; CloudFront reads images through Origin Access Control.
- No access keys in code or repo; nothing sensitive in logs.
- Pipeline credentials only as pipeline secrets/variables (GitLab CI/CD variables or GitHub Actions secrets); sandbox accounts expire after 8 hours, so they are refreshed, never committed.

## 7. What breaks first at 10x load (for the interview)

1. Single EC2 instance: CPU saturates first -> Auto Scaling group across 2+ AZs.
2. DynamoDB hot key on a very popular event's RSVP counter -> shard the counter or queue RSVPs.
3. Origin load on cache misses -> longer TTLs, cache-key tuning.
4. Cost: CloudFront and DynamoDB on-demand grow with traffic; set billing alarms.

## 8. Risks and assumptions

| Risk | Mitigation |
|---|---|
| AWS access arrives late | Finish the board and repo first; keep diagram and README work independent of AWS |
| Single operator on every workstream | Sequence the issues so each step is independently verifiable; the pipeline re-checks every invariant on each push |
| Scope creep | Anything outside section 3 "Real" stays design-only |
| DynamoDB adds build time | Fallback: in-memory stub, keep DynamoDB in the diagram and explain it |
| IaC and pipeline add build time and are verified automatically | One small CloudFormation template set, a health-check step, test in a sandbox account before the final run |
| Sandbox credentials expire after 8 hours | Refresh the pipeline credentials when a sandbox expires |

## 9. Execution plan

| # | Issue | Milestone | Owner | Acceptance criteria |
|---|---|---|---|---|
| 1 | Inception doc | Inception | Artur | Committed, reviewed against the brief |
| 2 | Architecture diagram | Inception | Artur | Hand-drawn, official AWS icons, shows flows from section 4 |
| 3 | DynamoDB table + S3 bucket + IAM role | Construction | Artur | Role limited to table and bucket prefix |
| 4 | EC2 instance + deploy stub app | Construction | Artur | `/events` responds on the instance |
| 5 | ALB + CloudFront | Construction | Artur | Public CloudFront URL returns `/events` |
| 6 | Stub app: events, RSVP, presigned upload | Construction | Artur | Capacity check and 5 MB limit work |
| 7 | Smoke test checklist and screenshots | Construction | Artur | Checklist of flows passed on the deployed endpoint |
| 8 | README | Construction | Artur | Description, setup, pipeline documentation |
| 9 | 10-minute demo slides + rehearsal | Operations | Artur | Dry run done, Q&A answers prepared |
| 10 | Verify deployment and run the verification self-check | Operations | Artur | Self-check run, weak answers fixed |
| 11 | Dual CI: the same pipeline on GitLab CI and GitHub Actions | Operations | Artur | Both providers run the shared `ci/` scripts green on every push to `main` |

## 10. Decisions taken and open questions

Decisions taken:
- Admin dashboard and email reminders are design only; event CRUD, RSVP and upload are stubs.
- 500 concurrent users is a design target, not load-tested.
- Cost ceiling about $20 for the week; the cleanup step deletes everything.
- No write authentication in the poc stub (Cognito is the named next step).
- HTTPS through the CloudFront default certificate only; no custom domain.

Still open:
- Does the sandbox account allow CloudFront and DynamoDB without limits?
- Is DynamoDB acceptable as the data store?
