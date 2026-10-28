# Project Cost Estimator Pipeline

An automated, intelligent multi-agent pipeline designed to parse software and hardware architecture diagrams, extract system requirements, predict engineering effort through machine learning models, search live component market prices, and synthesize unified cost estimations.

---

## Architecture Diagram

```mermaid
flowchart TD
    subgraph InputLayer["1. Input Ingestion"]
        IMG["Architecture / Workflow Diagram (.png, .jpg)"]
    end

    subgraph VisionEngine["2. Visual Extraction & Schema Parsing"]
        GEMINI["Multimodal Vision Agent (Gemini Engine)"]
        PARSE["Component Extraction & Classification"]
        VAL["Pydantic Structural JSON Normalization"]
        GEMINI --> PARSE --> VAL
    end

    subgraph ParallelAgents["3. Parallel Multi-Agent Processing Track"]
        subgraph HWAgent["Hardware Agent Pipeline"]
            direction TB
            HW_BOM["Bill of Materials (BOM) Identification"]
            HW_SEARCH["Live Market Price Fetching (TinyFish / SerpAPI)"]
            HW_SUM["Hardware Unit Cost Aggregation"]
            HW_BOM --> HW_SEARCH --> HW_SUM
        end

        subgraph SWAgent["Software Agent & ML Engine"]
            direction TB
            SW_FP["Function Point (FP) & Role Heuristics"]
            SW_ML["Trained Effort Regressor (china_model.joblib)"]
            SW_EFFORT["Development Person-Hours Estimation"]
            SW_FP --> SW_ML --> SW_EFFORT
        end
    end

    subgraph SynthesisEngine["4. Neural Synthesis & Cost Aggregation"]
        NVIDIA["NVIDIA Nemotron LLM (llama-3.1-nemotron-70b / ultra)"]
        RULES["Context Merging & Risk Margin Computation"]
        FINAL_JSON["Final Cost Specification & Budget Breakdown (INR)"]
        NVIDIA --> RULES --> FINAL_JSON
    end

    subgraph PresentationLayer["5. Application Delivery"]
        APP["Flask Core Application (pipeline_app)"]
        DASH["Real-Time Phase Progress Tracking"]
        TABLES["Itemized Cost Tables & Budget Summary"]
        CHAT["Interactive Estimation Consultation Chatbot"]
        APP --> DASH
        APP --> TABLES
        APP --> CHAT
    end

    IMG --> GEMINI
    VAL -->|"Hardware Specs"| HW_BOM
    VAL -->|"Software Modules"| SW_FP
    HW_SUM --> NVIDIA
    SW_EFFORT --> NVIDIA
    FINAL_JSON --> APP

    classDef primary fill:#e8f0fe,stroke:#1a73e8,stroke-width:2px,color:#174ea6;
    classDef agent fill:#fef7e0,stroke:#f9ab00,stroke-width:2px,color:#b06000;
    classDef llm fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#0d652d;
    classDef ui fill:#f3e8fd,stroke:#9334e6,stroke-width:2px,color:#6200ee;

    class IMG primary;
    class GEMINI,PARSE,VAL primary;
    class HW_BOM,HW_SEARCH,HW_SUM,SW_FP,SW_ML,SW_EFFORT agent;
    class NVIDIA,RULES,FINAL_JSON llm;
    class APP,DASH,TABLES,CHAT ui;
```

---

## Detailed Dataflow Representation

```mermaid
sequenceDiagram
    autonumber
    actor User as Engineer / User
    participant Web as Web Dashboard (Flask)
    participant Vision as Vision Parser (Gemini)
    participant HW as Hardware Agent
    participant SW as Software Agent (ML)
    participant LLM as NVIDIA Nemotron

    User->>Web: Uploads Architecture Diagram
    Web->>Vision: Sends Image for Visual Decomposition
    Vision->>Vision: Detects Nodes, Databases, Protocols, Hardware
    Vision-->>Web: Emits Structured Component JSON
    
    par Hardware Track
        Web->>HW: Sends Identified Hardware Components
        HW->>HW: Live Market Search via TinyFish / SerpAPI
        HW-->>Web: Returns Hardware BOM & Part Costs
    and Software Track
        Web->>SW: Sends Functional Scope & Module Specs
        SW->>SW: Applies FP Heuristics & china_model.joblib
        SW-->>Web: Returns Role-Hours & Engineering Effort
    end

    Web->>LLM: Dispatches Combined Hardware + Software Telemetry
    LLM->>LLM: Synthesizes Final Total Project Valuation (INR)
    LLM-->>Web: Delivers Structured Budget Matrix
    Web-->>User: Visualizes Breakdown & Activates Advisor Chatbot
```

---

## Subsystem Overview

| Component | Directory | Description |
| :--- | :--- | :--- |
| Core Pipeline Dashboard | `pipeline_app/` | Central Flask controller coordinating execution, UI rendering, and AI chat. |
| Hardware Agent | `Hardware agent/` | Automates BOM mapping and live internet price lookup using TinyFish and SerpAPI. |
| Software Agent | `software agent/` | Computes functional size, role allocations, and effort heuristics. |
| ML Models & Datasets | `Models/` | Pretrained scikit-learn models, training notebook, and effort benchmark datasets. |
| Vision Engine | `Nvidea/` | Multimodal perception pipeline extracting entities and diagrams via vision models. |
| Test Bench | `TEST images/` | Validation diagrams and reference inputs for pipeline verification. |

---

## Repository Structure

```text
Team-4C.5/
|-- .gitignore
|-- README.md
|-- Hardware agent/
|   |-- .env.example
|   |-- .gitignore
|   |-- README.md
|   |-- app.py
|   |-- requirements.txt
|   `-- templates/
|       `-- index.html
|-- Models/
|   |-- SE_Cost_Estimation_Training_Evaluation.ipynb
|   |-- dataset/
|   |   |-- COCOMO-81.csv
|   |   |-- Desharnais.csv
|   |   |-- china.arff
|   |   `-- renzo-pomodoro.csv
|   |-- models/
|   |   |-- Actual.effort/
|   |   |-- Effort/
|   |   |-- TotalLaborCost/
|   |   `-- registry.json
|   `-- requirements.txt
|-- Nvidea/
|   |-- image.py
|   |-- key.txt.example
|   `-- parts_preview.png
|-- pipeline_app/
|   |-- README.md
|   |-- app.py
|   |-- local_model.py
|   |-- requirements.txt
|   |-- static/
|   |   |-- app.js
|   |   |-- script.js
|   |   `-- style.css
|   `-- templates/
|       `-- index.html
|-- software agent/
|   |-- README.md
|   |-- app.py
|   |-- requirements.txt
|   |-- role_hours.json
|   |-- work_items_fp.json
|   `-- templates/
|       `-- index.html
`-- TEST images/
    `-- .gitkeep
```

---

## Setup and Installation

### 1. Prerequisites
- Python 3.10 or higher
- Node.js (required for live web scraping via TinyFish CLI)

### 2. Dependency Installation
Install required Python libraries:
```bash
pip install flask openai joblib numpy pydantic pillow
```

Install the TinyFish CLI globally:
```bash
npm install -g tinyfish
```

### 3. Key Configuration
Configure environment variables or credential templates:
- Hardware Agent: Copy `Hardware agent/.env.example` to `Hardware agent/.env` and specify `SERPAPI_KEY`.
- NVIDIA / Vision Model: Copy `Nvidea/key.txt.example` to `Nvidea/key.txt` and insert your NVIDIA API key, or set the environment variable:
  - Linux / macOS:
    ```bash
    export GEMINI_API_KEY="your-api-key"
    ```
  - Windows (PowerShell / Command Prompt):
    ```bash
    setx GEMINI_API_KEY "your-api-key"
    ```

### 4. Running the Application
Launch the unified pipeline dashboard:
```bash
cd pipeline_app
python app.py
```

Access the interface in your browser:
```text
http://127.0.0.1:5002
```

---

## Machine Learning Models & Datasets

The repository includes model checkpoints and evaluation artifacts trained on standard software cost metrics:
- Datasets: China Dataset, COCOMO-81, Albrecht, Maxwell, NASA93, Desharnais, and Renzo-Pomodoro.
- Metrics & Targets: Actual Effort (Person-Hours), Function Points (FP), Man-Months (MM), and Total Labor Cost.
- Training Pipeline: Implemented in `Models/SE_Cost_Estimation_Training_Evaluation.ipynb` utilizing scikit-learn regressor pipelines with joblib serialization.
