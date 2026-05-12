# Thread Architecture

## Overview
The adapted bot will use a multi-threaded architecture to handle different aspects of commercial real estate listing processing and communication with owners.

## Thread Types

### 1. Parser Threads
- Responsible for scraping commercial real estate listings from Avito
- Each parser thread is associated with a specific AdsPower profile
- Threads run independently and communicate through the database
- Each thread handles:
  - Yandex warmup
  - Category browsing
  - Real estate listing search and parsing
  - Data extraction and storage in the database

### 2. Database Processing Thread
- Monitors the database for new listings
- Processes and classifies listings
- Identifies potential owners based on listing data
- Updates scoring for accounts and phones

### 3. Communication Threads
- Handles initial contact with property owners
- Manages dialogues with owners
- Updates dialog and message status in the database
- Implements anti-blocking measures and rate limiting

## Communication Flow
1. Parser threads scrape listings and store in database
2. Database processing thread analyzes listings and identifies owners
3. Communication threads initiate contact with owners
4. All threads coordinate through database locks to prevent duplication

## Database Locking Strategy
- Use database transactions for critical operations
- Implement row-level locking for listing processing
- Use application-level locks for complex operations
- Ensure thread-safe database access patterns

## Data Flow Diagram
```mermaid
graph TD
    A[AdsPower Profile] --> B[Parser Thread]
    B --> C[Database Storage]
    C --> D[Database Processing Thread]
    D --> E[Owner Identification]
    E --> F[Communication Threads]
    F --> G[Dialog Management]
    G --> H[Message Exchange]
    H --> C