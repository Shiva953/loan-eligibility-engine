# Loan Eligibility Engine

## Mission

Design, build, and deploy an automated system that ingests user data, discovers personal loan products from public websites, matches users to eligible products, and notifies them. This project tests the ability to integrate cloud services with a powerful, self-hosted workflow automation engine.

This assignment is objective-based. A framework is provided, but creative solutions and architectural decisions that improve performance, reduce cost, and demonstrate a deeper understanding of the problem space are highly valued.

## Core technologies

- **Backend language:** Python or Go
- **Cloud platform:** AWS (Lambda, RDS for PostgreSQL, SES for email). API Gateway is optional.
  - Local deployment is acceptable; public deployment on AWS is a plus, not a requirement.
  - You are not expected to incur cloud costs.
- **AI and workflow:**
  - A self-hosted n8n instance for workflow automation.
  - Use an LLM API you have access to for specific AI tasks. Paid access is not required.
- **Deployment:** Serverless Framework (for AWS resources) and Docker Compose (for n8n)

## Functional requirements

### 1. Scalable user data ingestion

- **Bulk CSV upload:** Design a robust and scalable mechanism for uploading a CSV file containing user data (e.g. `user_id`, `email`, `monthly_income`, `credit_score`, `employment_status`, `age`).
  - Note: a direct upload through a standard Lambda-backed API Gateway endpoint can hit request size and timeout limits. A more professional, event-driven pattern is expected.
- **Data persistence:** Once uploaded, the data must be parsed and stored in a dedicated table within an Amazon RDS for PostgreSQL database.

### 2. The n8n automation engine

Set up and configure a self-hosted n8n instance using Docker. All core business logic is orchestrated through n8n workflows. Design and build the following interconnected workflows.

#### Workflow A: Loan product discovery (the web crawler)

- **Goal:** Autonomously find and store personal loan product information.
- **Trigger:** Runs on a schedule (e.g. once daily).
- **Process:**
  - Crawl a list of public financial websites (choose 2–3 relevant sites).
  - Extract key details for personal loan products, such as `product_name`, `interest_rate`, and the eligibility criteria (e.g. minimum monthly income, required credit score range).
  - Store this structured data in a new `loan_products` table in the PostgreSQL database.
  - Handling changes in website layouts is optional.

#### Workflow B: User-loan matching (the eligibility filter)

- **Goal:** Match users from the database with the loan products they are likely eligible for.
- **Trigger:** Triggered via a webhook after a new CSV of users has been successfully processed.
- **Process:**
  - Fetch the newly added user records from the PostgreSQL database.
  - Fetch the entire list of available loan products and their criteria from the database.
  - Implement the core matching logic within n8n.
  - For every successful match, store the result (e.g. linking `user_id` to `product_id`) in a `matches` table.

#### Creative challenge: the optimization treasure hunt

- **Problem:** Filtering thousands of users against dozens of loan products can be slow. Using a powerful LLM (like Gemini/GPT) to evaluate every single user-product pair would be extremely slow and expensive.
- **Task:** Design and implement a multi-stage, optimized filtering pipeline. Show creativity here. How can the number of user-product pairs be reduced before using an LLM for qualitative checks? For example, use a fast SQL-based pre-filter followed by more complex checks in n8n, or explain another approach.

#### Workflow C: User notification

- **Goal:** Inform users about the loan products they have been matched with.
- **Trigger:** Runs after the matching process (Workflow B) is complete.
- **Process:**
  - For each user who has new matches, construct a personalized email.
  - Using the AWS SES node in n8n, send the email to a test inbox you control, listing the loan products the matched user may qualify for.
  - **Do not email the sample CSV addresses.** Use test-inbox aliases such as `abcd+one@gmail.com` and `abcd+two@gmail.com` (replace `abcd` with your own Gmail username) to prove the notification workflow works.
  - Show one received email in the demonstration video.

### 3. User interface

Provide a minimal UI that allows for the initial upload of the user data CSV file. This is the primary interaction point for starting the entire pipeline.

## Deliverables

### GitHub repository

A private repository containing all source code (Lambda functions, etc.) and configuration files, with these GitHub users invited as collaborators:

- `ujjwalkala-clickpe`
- `HarshSrivastav-Dailype`
- `ManishBiswal-clickpe`
- `tushar-clickpe`

### Infrastructure and automation files

- A `serverless.yml` file to deploy all required AWS resources.
- A `docker-compose.yml` file for launching the self-hosted n8n instance.
- The JSON files for each of the n8n workflows.

### Comprehensive documentation (README.md)

- **Architecture diagram:** A clear diagram showing how AWS services and the n8n workflows all interact.
- **Setup and deployment:** Step-by-step instructions to deploy the AWS stack and launch the n8n container. This must include how to configure all necessary credentials (AWS, database) within n8n.
- **Design decisions:** A brief explanation of the workflow design, web crawling strategy, and especially the solution to the optimization treasure hunt.

### Demonstration video (5–10 minutes)

- A walkthrough of the n8n workflows, explaining the logic of each node.
- A live, end-to-end demonstration of the entire pipeline.
- Show an email received in a test inbox after the pipeline completes.

## Submission

_(No submission details were included in the brief.)_

## Evaluation criteria

| Category | Max points | Description |
|---|---|---|
| n8n workflow design and automation | 30 | Sophistication, robustness, and efficiency of the n8n workflows. Creative use of n8n nodes for crawling, data manipulation, and logic. The solution to the optimization treasure hunt is a key factor. |
| Cloud architecture and integration | 20 | Quality of the AWS serverless design. Seamless and secure integration between AWS services and the self-hosted n8n instance. Scalability of the data ingestion method. |
| Backend functionality and code quality | 30 | Correct implementation of the data ingestion pipeline. Clean, modular, and well-commented code. |
| Documentation and presentation | 20 | Clarity of the architecture diagram and README.md. Quality and comprehensiveness of the video demonstration. |
| **Total** | **100** | |

## Notes

- **Sample data (`users.csv`):** Download the CSV and use it to test the solution: https://drive.google.com/file/d/1KBaZRkfr7T2kVBKSr_2fr7v2tTe6f8qu/view?usp=sharing