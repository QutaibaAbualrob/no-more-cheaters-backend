# No More Cheaters — Final Plan

> **Deadline:** June 17
> **Goal:** Live demo on AWS. Professor clicks through a URL and sees a complete system.
> **Strategy:** Real where visible. Lite where it saves time.

---

## ⚠️ CRITICAL RULE FOR ALL AGENTS — READ BEFORE MODIFYING

**NEVER edit, overwrite, delete, or modify any existing content in this file.**

All changes — whether completing a task, adding new information, or updating the plan — must follow this process:

1. **Complete the task** as described in the relevant section
2. **Add a new entry** to the **CHANGES section** at the bottom of this file with:
   - Date
   - What was done
   - Which task was completed
3. **Do NOT** touch any text above the CHANGES section

If you need to record progress or add information:
- Add it to the **CHANGES section** at the bottom
- Never rewrite, replace, or modify existing sections

**Violating this rule will break the plan and waste the team's time. The original plan is the single source of truth — changes are logged, not overwritten.**

---

## 1. Architecture

```
Frontend (Vercel)                  Backend (AWS EC2 — GPU)
┌──────────────────┐               ┌──────────────────────────────┐
│  React 19 + Vite  │ ─── HTTPS →  │  Django 5.2 + DRF            │
│  allauth headless │              │  Gunicorn + Nginx            │
│  session + CSRF   │              │  Certbot (LetsEncrypt HTTPS) │
└──────────────────┘               │                              │
                                   │  RDS (MySQL) ←── DB          │
                                   │  EC2 volume ←── video files  │
                                   │  Resend ←── email API        │
                                   │  Redis ←── async queue       │
                                   │  YOLO ←── AI analysis        │
                                   │  rqworker ←── background     │
                                   └──────────────────────────────┘
```

| Service | What | Cost |
|---|---|---|
| Vercel | Frontend hosting (`nomorecheater.site` — bought via Vercel) | Free |
| AWS EC2 GPU | Django + YOLO + Redis + video storage | ~$50-100 (only running for demo week) |
| AWS RDS | MySQL database | ~$15/mo (only running for demo week) |
| Resend | Email API (Free tier) | $0 (3,000/mo) |
| Domain | `nomorecheater.online` (Namecheap) | ~$10-15/yr |
| LetsEncrypt | HTTPS certificate | Free |

---

## 2. Database Schema (MySQL on RDS)

### User & Authentication

| Table | Key Fields | Notes |
|---|---|---|
| **apis_user** | id (UUID PK), email (unique), username, password, role (ADMIN/DEAN/INSTRUCTOR), is_active, is_superuser, created_at | Custom user model, email-based login. Role controls access. UUID prevents ID enumeration. |
| **apis_userpreferences** | user (FK→user, unique), email_notifications, dashboard_alerts, preferred_language, timezone, theme, metadata (JSON) | One row per user. Metadata stores per-user AI threshold overrides. |

### Exam Hierarchy

| Table | Key Fields | Notes |
|---|---|---|
| **apis_exam** | id (UUID PK), instructor (FK→user), name, description, created_at, updated_at | Logical container for exam sessions. Belongs to one instructor. |
| **apis_examsession** | id (UUID PK), exam (FK→exam), student_identifier, status (PENDING/PROCESSING/COMPLETED/FAILED), created_at, updated_at | One per student per exam. Tracks lifecycle from upload through AI analysis. Unique constraint: one student per exam. |
| **apis_video** | id (UUID PK), session (FK→session, unique), file (FileField→S3/disk), original_filename, content_type, size_bytes, file_hash (unique SHA-256), duration_seconds, expires_at, uploaded_at | The uploaded recording. SHA-256 hash prevents duplicate uploads. File stored on EC2 disk (dev) or S3 (prod). 30-day expiry policy. |

### AI Analysis

| Table | Key Fields | Notes |
|---|---|---|
| **apis_analysisjob** | id (UUID PK), session (FK→session, unique), status (QUEUED/PROCESSING/COMPLETED/FAILED), ai_model_version, frame_sample_rate, started_at, completed_at, error_message, metadata (JSON) | Tracks one analysis run per session. Worker picks QUEUED jobs, processes frames, produces alerts + report. |
| **apis_alert** | id (UUID PK), session (FK→session), timestamp_sec (video time), behavior_type (PHONE_DETECTED/LAPTOPS/LOOKING_AWAY/OTHER), severity (LOW/MEDIUM/HIGH), confidence_score (0.0–1.0), metadata (JSON), snapshot_url, is_reviewed, reviewed_by (FK→user), reviewed_at, created_at | One alert per detected event (not per frame). Multiple alerts per session. Temporal merging reduces noise. Instructors mark as reviewed. |
| **apis_report** | id (UUID PK), session (FK→session, unique), overall_cheating_probability (0.0–1.0), total_alerts, alerts_by_type (JSON), processing_time_seconds, summary, generated_at | One report per completed analysis. Aggregates all alerts into summary. Probability derived from alert count + confidence scores. |

### System

| Table | Key Fields | Notes |
|---|---|---|
| **apis_auditlog** | id (UUID PK), user (FK→user, nullable), action (LOGIN/LOGOUT/VIDEO_UPLOADED/ANALYSIS_STARTED/ANALYSIS_COMPLETED/etc.), target_resource, ip_address, user_agent, metadata (JSON), performed_at | Immutable append-only log. No add/change/delete in admin. Dual-path logging: views + background workers. |
| **apis_systemsettings** | setting_key (PK), setting_value, description, updated_by (FK→user), updated_at | Key-value config store. Holds AI threshold defaults. Admin-only writes, audited. |

### Entity Relationships (Simplified)

```
User ──1:1── UserPreferences
  │
  └── Exam ──1:N── ExamSession ──1:1── Video
                    │    │
                    │    └── AnalysisJob (1:1 — job lifecycle)
                    │
                    ├── Alert (1:N — detected events)
                    │
                    └── Report (1:1 — aggregated summary)

AuditLog (standalone — references User but not owned by any entity)
SystemSettings (standalone — key-value store)
```

## 3. Day-by-Day Tasks

### Day 1 — Backend Core + AI Pipeline

#### Task 1.1 — Add DEAN Role

- Add `DEAN` to `User.Role` choices in `apis/models.py`
- Add `is_dean()` check to `apis/selectors.py`
- Create Django migration
- Create simple DEAN-gated list views in `apis/views.py` (instructor oversight, hall management)
- Add DEAN routes to `apis/urls.py`

**Depends on:** Nothing
**Team:** Backend

#### Task 1.2 — Add LAPTOPS Behavior Type

- Add `LAPTOPS` to `Alert.BehaviorType` choices in `apis/models.py`
- Create Django migration
- Update any serializers or display logic that references behavior types

**Depends on:** Nothing
**Team:** Backend

#### Task 1.3 — Build AI Module

Create `apis/ai/` package with these components, as outlined in the AI pipeline plan (`plans/latest.md`):

**`frame_extractor.py`** — Video frame reader
- Opens video files using OpenCV
- Reads video metadata: FPS, total frames, duration
- Samples frames at a configurable rate (default: every 15th frame)
- Returns each sampled frame with its timestamp and frame number
- Handles different video formats (mp4, mov, avi, etc.)

**`yolo_detector.py`** — Object detection with YOLO11x
- Loads the pretrained `yolo11x.pt` model (downloads weights on first run)
- Runs inference on each incoming frame
- Filters detections to only two target COCO classes:
  - Class 67 — `cell phone` → maps to `PHONE_DETECTED`
  - Class 63 — `laptop` → maps to `LAPTOPS`
- Returns detections with: type, confidence score (0.0–1.0), bounding box coordinates

**`pose_analyzer.py`** — Head pose estimation with YOLO11x-pose
- Loads the pretrained `yolo11x-pose.pt` model
- Extracts body keypoints for each person in the frame (nose, eyes, shoulders)
- Computes head orientation: compares nose position relative to eye midpoint
- If the head is turned beyond a threshold → flag as `LOOKING_AWAY`
- Returns detection with confidence score
- Only flags if the looking-away posture is sustained (heuristic, not ML-based)

**`detector.py`** — Main orchestrator
- Receives the video file path and configuration (sample rate, confidence thresholds)
- Manages the full pipeline:
  1. Open video via frame_extractor
  2. For each sampled frame, run yolo_detector and pose_analyzer in parallel
  3. Collect all raw frame-level detections with timestamps
- Runs a **temporal rules layer** to merge frame detections into consolidated events:
  - Same detection class within a 5-second sliding window → single event
  - Event confidence = highest confidence across merged frames
  - Events shorter than 2 seconds are dropped (noise filter)
- Generates an **annotated video** — a new MP4 file with visual indicators drawn on frames where events occurred:
  - Bounding boxes around detected objects (phone, laptop)
  - Labels and confidence scores displayed
  - Head pose indicators for looking-away events
  - Original video duration preserved, only annotated frames modified
- Returns:
  - List of consolidated alert events
  - Path to the annotated video file
  - Processing metadata (total events by type, total processing time)

**One-time model download:**
- Download `yolo11x.pt` and `yolo11x-pose.pt` when the module is first used (~220MB total)
- Weights cached on disk for subsequent runs

**Don't build:** Audio analysis, eye tracking / gaze vector estimation, custom model training, student identity / seat mapping, real-time streaming.

#### Task 1.4 — Update tasks.py

- Replace the `build_demo_report` placeholder call in `run_analysis()` with the real AI pipeline from Task 1.3
- Keep existing job lifecycle (QUEUED → PROCESSING → COMPLETED / FAILED)
- Keep existing audit logging (ANALYSIS_STARTED, ANALYSIS_COMPLETED, REPORT_GENERATED)
- If real analysis fails: let it fail naturally — job marks as FAILED, error message stored, session flips to FAILED

**Depends on:** Task 1.3
**Team:** Backend

#### Task 1.5 — Pre-analyze Demo Videos

- Run the AI pipeline on 2-3 short video clips (30-60 seconds each)
- Save generated alerts and reports to the database
- These load instantly during demo — no wait time
- Test live analysis of a new upload during demo so professor sees the real pipeline work

**Depends on:** Task 1.4
**Team:** Backend

---

### Day 2 — Frontend Wiring

#### Task 2.1 — Wire Video Upload API Module

- **`api/videos.ts`** — Replace all localStorage calls with real HTTP calls:
  - Upload: `POST /api/videos/upload/` with FormData
  - List: `GET /api/videos/`
  - Detail: `GET /api/videos/<id>/`
  - Analyze: `POST /api/videos/<id>/analyze/`
  - History: `GET /api/history/`
  - Annotated video URL: from video detail response

**Depends on:** Nothing (backend endpoints already exist)
**Team:** Frontend

#### Task 2.2 — Rewrite VideoProcessing Page

- File input for selecting a video file
- Upload button with loading indicator
- Video list showing uploaded videos with status badges (PENDING / PROCESSING / COMPLETED / FAILED)
- "Analyze" button on each pending video
- Progress indicator during analysis
- Results section: cheating probability gauge, alert list with timestamps and severity, annotated video player
- Handle both immediate response (200) and async polling (202) from the analyze endpoint

**Depends on:** Task 2.1
**Team:** Frontend

#### Task 2.3 — Rewrite AnalysisReports Page

- Fetch report and alerts from the real API
- Display cheating probability as a visual gauge
- Alert list: timestamp, behavior type, severity badge, confidence score
- Toggle to mark alerts as reviewed / unreviewed
- Video player showing the annotated MP4 with detection overlays

**Depends on:** Task 2.1
**Team:** Frontend

#### Task 2.4 — Wire AIThresholds Page

- Replace localStorage calls with `api/thresholds.ts` API calls
- `GET /api/thresholds/` on page load to fetch current values
- `PATCH /api/thresholds/me/` when slider values change
- Display global defaults vs user overrides clearly

**Depends on:** Nothing (api/thresholds.ts already exists)
**Team:** Frontend

#### Task 2.5 — Wire SystemLogs and SystemMetrics Pages

- **`api/system.ts`** — Replace hardcoded empty stubs with real API calls:
  - `GET /api/system/logs/` with pagination
  - `GET /api/system/metrics/`
- **`SystemLogs.tsx`** — Display paginated table: timestamp, action, actor email, details
- **`SystemMetrics.tsx`** — Display stats cards: total users, videos, analyses, platform info

**Depends on:** Nothing (backend endpoints already exist)
**Team:** Frontend

#### Task 2.6 — Wire Dashboard

- Replace hybrid mock/real data with full API calls
- Use `getDashboardStats()` and `getDashboardActivity()` from `api/dashboard.ts`

**Depends on:** Nothing (api/dashboard.ts already exists)
**Team:** Frontend

#### Task 2.7 — Wire History Page

- Replace localStorage data with `getHistory()` from `api/videos.ts`
- Display table of completed videos with report summaries and links to view details

**Depends on:** Task 2.1
**Team:** Frontend

#### Task 2.8 — Wire DEAN Frontend Pages

- Connect `InstructorOversight.tsx` and `HallManagement.tsx` to the DEAN backend endpoints from Task 1.1
- Ensure `RequireRole` component properly handles the DEAN role

**Depends on:** Task 1.1
**Team:** Frontend

---

### Day 3 — Deployment (Infrastructure)

#### Task 3.1 — Launch EC2 GPU Instance

- Launch GPU instance (g4dn.xlarge or similar) with Ubuntu 22.04
- Install: Python 3, pip, venv, nginx, mysql-client, Redis, OpenCV system dependencies
- Install CUDA + cuDNN for GPU-accelerated YOLO inference
- Allocate and attach a static IP (Elastic IP)
- Clone the backend repository
- Create virtualenv, install Python dependencies (including ultralytics, opencv-python, django-rq, redis, gunicorn, django-storages)
- Download YOLO model weights

**Depends on:** Nothing
**Team:** DevOps/Backend

#### Task 3.2 — Set Up RDS MySQL

- Launch RDS MySQL instance (db.t3.micro)
- Create database and user credentials
- Configure security group to allow connections from EC2 only
- Verify connection from EC2

**Depends on:** Nothing
**Team:** DevOps

#### Task 3.3 — Set Up Redis

- Install Redis on EC2 (already installed in Task 3.1 if combined)
- Configure Redis to start automatically on boot
- Update Django `settings.py` with Redis URL for django-rq

**Depends on:** Task 3.1
**Team:** DevOps

#### Task 3.4 — Configure Django for Production

- Create `.env` file with: `DJANGO_SECRET_KEY`, RDS credentials, Resend API key, Redis URL, domain name
- Change database from SQLite to MySQL
- Change `EMAIL_BACKEND` from console to Resend SMTP
- Run `python manage.py collectstatic` and configure Nginx to serve static files
- Configure `CORS_ALLOWED_ORIGINS` to include Vercel frontend domain
- Run migrations on RDS
- Create superuser accounts (admin, dean, instructor)

**Depends on:** Tasks 3.1, 3.2, 3.3
**Team:** Backend/DevOps

#### Task 3.5 — Set Up Nginx + Gunicorn

- Configure Gunicorn to serve Django on localhost:8000
- Create systemd service for Gunicorn (auto-restart on reboot + crash)
- Configure Nginx as reverse proxy: port 80 → localhost:8000
- Configure Nginx to serve Django static files and uploaded media files
- Test: visit EC2 IP in browser, see Django app running

**Depends on:** Task 3.4
**Team:** DevOps

#### Task 3.6 — Set Up HTTPS with Certbot

- Install Certbot on EC2
- Point domain DNS to EC2 Elastic IP
- Run Certbot to obtain LetsEncrypt SSL certificate for the domain
- Configure Nginx to use HTTPS (port 443) and auto-redirect HTTP to HTTPS
- Set up auto-renewal for the certificate
- Verify: `https://nomorecheater.online` loads with a valid padlock

**Depends on:** Task 3.5, domain purchased and pointed to EC2
**Team:** DevOps

#### Task 3.7 — Seed Demo Data

- Write and run seed script to create:
  - 3 user accounts: admin, dean, instructor (known passwords)
  - 2 exams with pre-analyzed sessions (alerts + reports stored in DB)
- Verify data displays correctly through the frontend

**Depends on:** Task 3.4
**Team:** Backend

#### Task 3.8 — Deploy Frontend to Vercel

- Connect Vercel to the GitHub repository
- Set `VITE_API_URL` to `https://nomorecheater.online`
- Configure domain: `nomorecheater.site` points to backend at `nomorecheater.online`
- Verify `npm run build` passes and the deployed site loads

**Depends on:** Task 3.6 (backend must be reachable at the domain)
**Team:** Frontend

#### Task 3.9 — Configure Resend + Domain Email

- Add domain to Resend dashboard
- Configure DNS records: SPF, DKIM, MX for email verification
- Wait for DNS propagation (5-10 minutes)
- Send test email to verify: password reset arrives in Gmail inbox

**Depends on:** Task 3.4 (email backend configured), domain purchased
**Team:** DevOps

#### Task 3.10 — Full Integration Test

- Visit the live URL from a clean browser (not localhost)
- Sign up / sign in with all 3 roles
- Upload a video file
- Run analysis and view results
- Play the annotated video with detection overlays
- Check audit logs for entries
- Test password reset — email arrives in Gmail
- Verify HTTPS padlock shows secure

**Depends on:** Tasks 3.7, 3.8, 3.9
**Team:** Everyone

---

### Day 4 — Polish + Report

#### Task 4.1 — Take Screenshots

Capture every screen from the live URL:
- Login page, Dashboard with stats, Video upload form, Video list with status badges
- Analysis results: probability gauge, alert list, annotated video player
- AI Thresholds sliders, System Logs table, System Metrics cards
- Manage Users, Profile page
- Password reset email sitting in Gmail inbox

**Depends on:** Task 3.10
**Team:** Frontend

#### Task 4.2 — Update final_docu.txt

- Read the existing `docu.txt`
- Update only sections where content has changed:
  - Chapter 4 (System Design): DEAN role, YOLO AI implementation (3 behaviors), annotated video output, MySQL database, AWS deployment architecture, Resend email
  - Chapter 5 (Conclusions): what was actually implemented, screenshots, test results
- All updates are appended at the bottom in a CHANGES section
- No deletions or edits to original content

**Depends on:** Task 4.1
**Team:** Whoever owns the report

#### Task 4.3 — Practice Demo

- Run through the 5-minute demo script 2-3 times
- Fix any issues, broken flows, or awkward transitions
- Make sure the annotated video plays correctly and detection overlays are visible

**Depends on:** Task 3.10
**Team:** Everyone

---

## 4. Task Dependencies

```
Day 1
├── Task 1.1 (DEAN backend)
├── Task 1.2 (LAPTOPS type)
├── Task 1.3 (AI module)
│
├── Task 1.4 (Update tasks.py) ← needs 1.3
├── Task 1.5 (Pre-analyze videos) ← needs 1.4

Day 2
├── Task 2.1 (API module - videos)
├── Task 2.4 (Thresholds) — no deps
├── Task 2.5 (Logs/Metrics) — no deps
├── Task 2.6 (Dashboard) — no deps
│
├── Task 2.2 (VideoProcessing page) ← needs 2.1
├── Task 2.3 (AnalysisReports page) ← needs 2.1
├── Task 2.7 (History page) ← needs 2.1
├── Task 2.8 (DEAN pages) ← needs 1.1

Day 3
├── Task 3.1 (EC2 GPU instance)
├── Task 3.2 (RDS MySQL)
│
├── Task 3.3 (Redis) ← needs 3.1
├── Task 3.4 (Django config) ← needs 3.1 + 3.2 + 3.3
├── Task 3.5 (Nginx + Gunicorn) ← needs 3.4
├── Task 3.6 (HTTPS Certbot) ← needs 3.5 + domain
├── Task 3.7 (Seed data) ← needs 3.4
├── Task 3.8 (Vercel deploy) ← needs 3.6
├── Task 3.9 (Resend config) ← needs 3.4 + domain
│
├── Task 3.10 (Integration test) ← needs 3.7 + 3.8 + 3.9

Day 4
├── Task 4.1 (Screenshots) ← needs 3.10
├── Task 4.2 (Update report) ← needs 4.1
├── Task 4.3 (Practice demo) ← needs 3.10
```

**What can run in parallel (same day):**
- Day 1: Tasks 1.1, 1.2, 1.3 can run simultaneously
- Day 2: Tasks 2.1, 2.4, 2.5, 2.6 can run simultaneously
- Day 3: Tasks 3.1, 3.2 can run simultaneously

---

## 5. Things You Need to Know

### build_demo_report

A function in `apis/services.py` that currently generates fake analysis results. Creates one hardcoded LOOKING_AWAY alert at 30 seconds with 72% confidence — identical every time. This is the placeholder that Task 1.4 replaces with the real YOLO pipeline.

### Redis + Async Queue

When you click "Analyze," the video processing can either:
- **Synchronous (no Redis):** The HTTP request blocks until YOLO finishes. Browser sits loading for 30-60 seconds.
- **Async (with Redis):** The request returns instantly with status "QUEUED." A background worker (`rqworker`) picks up the job and processes it. The frontend polls for completion.

We're using async + Redis so the demo flow is: click Analyze → "processing" appears instantly → results load when ready. Better experience.

### Static Files on EC2

Django admin needs CSS/JS files. `python manage.py collectstatic` gathers them into one folder. Nginx must serve this folder. Without it, the admin panel loads without styling. Simple 10-minute Nginx config.

### HTTPS via Certbot

Vercel serves frontend over HTTPS. EC2 backend must also use HTTPS or the browser will block mixed content (videos, API calls). Solution:
- Buy a domain (same one used for Resend)
- Point it to EC2 Elastic IP
- Run Certbot → auto-configures Nginx with LetsEncrypt certificate
- Free, automated renewal, takes 30 minutes total

### Video Storage

- Videos are stored on the EC2 instance's local disk (not S3)
- Django serves them through Nginx at `/media/`
- Frontend receives a URL like `https://nomorecheater.online/media/exam-videos/uuid/file.mp4`
- No localStorage — browser storage is too small for video files

### GPU vs CPU Inference

- GPU (CUDA): YOLO processes frames in ~1-5 seconds per minute of video
- CPU: YOLO processes frames in ~30-60 seconds per minute of video
- We're using a GPU instance for acceptable demo performance
- Pre-analyze 2-3 demo videos so they load instantly
- Live analysis during demo still runs in a few seconds

### Annotated Video

After analysis, the AI module produces a new MP4 file with visual indicators (bounding boxes, labels) drawn on frames where detections occurred. This file is:
- Stored in Django media directory on EC2
- Frontend receives a URL to play it in an HTML5 video player
- Professor can scrub through and see the AI detection results visually

---

## 6. Demo Script (5 Minutes)

1. **0:00** — Login as instructor. Dashboard shows real stats (videos, alerts, analyses).
2. **0:30** — Video list shows pre-analyzed entries with COMPLETED status. Click Analyze on a pending video.
3. **1:00** — Results load: cheating probability gauge, alert list by type and severity. Play the annotated video — professor sees detection boxes overlaid on the footage in real time.
4. **2:00** — AI Thresholds: move a slider (e.g. gaze threshold). Setting saves to server immediately.
5. **2:30** — System Logs: paginated table showing real audit entries (login, upload, analyze, complete).
6. **3:00** — Switch to admin account. Show Manage Users, System Metrics with platform stats.
7. **4:00** — Password Reset: trigger email, open Gmail inbox — email arrived from the custom domain via Resend.
8. **5:00** — Close: deployed on AWS with GPU, real YOLO AI, HTTPS, role-based access, source code available.

---

## 7. CHANGES

*This section tracks all updates to this plan. No content above is modified — only additions below.*

| Date | Change |
|---|---|
| June 13 | Initial plan created |
| June 13 | Domain added: `nomorecheater.online` via Namecheap |
| June 13 | Frontend domain set: `nomorecheater.site` bought via Vercel |
| June 13 | GPU instance confirmed available — no limit issue |
| June 13 | LAPTOPS behavior kept as-is (intentional) |
| June 13 | YOLO failure behavior: let it fail naturally (no fallback) |
| June 13 | AI module section expanded with detailed component descriptions from `plans/latest.md` |
| June 13 | Database schema section added (10 tables, entity relationships) |
| June 13 | Added CRITICAL RULE warning at top — agents must never modify existing content, only append to CHANGES section |
