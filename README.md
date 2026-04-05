# Comp Circuit  
## A real-time climbing competition platform

Comp Circuit is a mobile-first web app designed for indoor climbing competitions.  
It enables live scoring, dynamic leaderboards, and competitor tracking all in one streamlined system.

## Overview

Running climbing comps is challenging. Keeping track of points for competitors,
how much climbs are worth, the amount of attempts each competitor takes, and then 
adding in the laborious task of entering all of that information into a speadsheet or 
calulating by hand to find the finalists can be exhausting.  

Comp Circuit fixes that.

Instead of paper scorecards and manual rankings, competitors can log climbs quickly and easily while organisers get instant results and insights.

## Competitor View Demo

![Competitor View Demo](Docs/Images/competitor_view_demo.gif)

## Admin Map Setup

![Admin Map Setup Demo](Docs/Images/admin_map_setup.gif)

### Live Competition Scoring

- Log climbs instantly from your phone  
- Designed for efficient logging and instant leaderboard updates 

### Interactive Climb Map

- Visualise climbs by section  
- Filter by colour / category  
- Click on a climb represented on the map → scrolls directly to scoring card  

### Dynamic Leaderboards

- Categories: All, Male, Female, Inclusive, Doubles  
- Score based on top-N climbs  
- Attempts used as tie-breaker  

### Competitor Stats

- Track performance throughout the competition
- Shows the competitors what they have and have not attempted

### Admin + Route Setter Tools

- Manage competitions  
- Edit climbs and sections  
- Control scoring behaviour  

##  Tech Stack

Backend:       Python (Flask)  
Database:     PostgreSQL  
Frontend:      HTML, CSS, JavaScript   

## Local Setup

git clone https://github.com/nblawler/UrbanClimbCompApp.git

SETUP

1. Clone Git
   git clone https://github.com/nblawler/UrbanClimbCompApp.git
2. Setup Environment
   python3 -m venv .venv
   source .venv/bin/activate

3. Install Dependencies
   pip install -r requirements.txt

For Local Development

1. Run App

   python -m app.run --port 5000

2. Access at http://127.0.0.1:5000
