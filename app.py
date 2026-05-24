"""
Flask application factory and entry point.
"""

import os
from dotenv import load_dotenv
from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from models import db, init_db

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def create_app():
    app = Flask(__name__)

    # Database configuration
    db_path = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'scheduler.db')}",
    )
    # Render's PostgreSQL uses 'postgres://' but SQLAlchemy requires 'postgresql://'
    if db_path.startswith("postgres://"):
        db_path = db_path.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_path
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # JWT configuration
    jwt_secret = os.environ.get("JWT_SECRET_KEY")
    if not jwt_secret:
        raise ValueError("No JWT_SECRET_KEY set for Flask application. Please set it in your .env file or environment.")
    app.config["JWT_SECRET_KEY"] = jwt_secret
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = 86400  # 24 hours

    # Initialize extensions
    db.init_app(app)
    JWTManager(app)
    CORS(app, origins=["http://localhost:3000", "http://localhost:3001", "https://doctor-backend-mzvn.onrender.com"], supports_credentials=True)

    # Register blueprints
    from routes.auth import auth_bp
    from routes.doctors import doctors_bp
    from routes.leaves import leaves_bp
    from routes.schedule import schedule_bp
    from routes.special_requests import special_requests_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(doctors_bp, url_prefix="/api/doctors")
    app.register_blueprint(leaves_bp, url_prefix="/api/leaves")
    app.register_blueprint(schedule_bp, url_prefix="/api")
    app.register_blueprint(special_requests_bp, url_prefix="/api/special-requests")

    # Ensure data directory exists BEFORE initializing database
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    # Initialize database
    init_db(app)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
