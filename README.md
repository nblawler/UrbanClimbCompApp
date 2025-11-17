# Urban Climb Comp App

File Overview
1. app.py 
    This is the main Flask application that handles the routes, scoring etc.
2. templates/index.html 
    This is the homepage when competitors enter their competitor number
3. templates/competitor.html
    This is the competitor dashboard where the competitor can enter their scores
4. templates/admin.html
    This is the page for admin control over removing competitors from the db. To remove competitors from the database go to http://127.0.0.1:5001/admin
    Then using the password found in app.py under ADMIN_PASSORD remove one-by-one or all competitors at once to fully reset the db.
5. static/app.css
    This is the style sheet for all of the pages
6. requirments.txt
    This is the dependency list for pip instalation


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
    python app.py --port 5001

2. Access at http://127.0.0.1:5001