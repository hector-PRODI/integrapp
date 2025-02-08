from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy.exc import IntegrityError
from sqlalchemy import update, event, types
from sqlalchemy.types import TypeDecorator, Text
from flask_session import Session
from functools import wraps
import msal
import uuid
import re
import os
import hashlib
import traceback
import json
import random
from datetime import datetime
from werkzeug.utils import secure_filename
import requests

app = Flask(__name__)

# Azure AD Configuration
app.config.update({
    'SESSION_TYPE': 'filesystem',
    'AZURE_CLIENT_ID': '',  # From Azure Portal registration
    'AZURE_CLIENT_SECRET': '',  # From Azure Portal registration
    'AZURE_TENANT_ID': '',  # From Azure Portal
    'AZURE_AUTHORITY': 'https://login.microsoftonline.com/',
    'AZURE_REDIRECT_PATH': '/getAToken',  # Redirect URI registered in Azure Portal
    'SCOPE': [
        'https://graph.microsoft.com/User.Read',
        'https://graph.microsoft.com/User.Read.All',
        'https://graph.microsoft.com/email',
        'https://graph.microsoft.com/profile'
    ],
    'ENDPOINT': 'https://graph.microsoft.com/v1.0/me'  # Microsoft Graph API endpoint
})

# Other app configurations
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'bin'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

Session(app)  # Initialize Flask-Session

# Helper functions for Azure AD
def load_cache():
    cache = msal.SerializableTokenCache()
    if session.get("token_cache"):
        cache.deserialize(session["token_cache"])
    return cache

def save_cache(cache):
    if cache.has_state_changed:
        session["token_cache"] = cache.serialize()

def build_msal_app(cache=None):
    return msal.ConfidentialClientApplication(
        app.config['AZURE_CLIENT_ID'],
        authority=app.config['AZURE_AUTHORITY'],
        client_credential=app.config['AZURE_CLIENT_SECRET'],
        token_cache=cache
    )

def get_token_from_cache(scope=None):
    cache = load_cache()
    cca = build_msal_app(cache)
    accounts = cca.get_accounts()
    if accounts:
        result = cca.acquire_token_silent(scope, account=accounts[0])
        save_cache(cca.token_cache)
        return result

# Decorator definition
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

# Authentication routes
@app.route("/login")
def login():
    session["flow"] = build_msal_app().initiate_auth_code_flow(
        app.config['SCOPE'],
        redirect_uri=url_for("authorized", _external=True)
    )
    return redirect(session["flow"]["auth_uri"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect(
        app.config['AZURE_AUTHORITY'] + "/oauth2/v2.0/logout" +
        "?post_logout_redirect_uri=" + url_for("index", _external=True)
    )

@app.route(app.config['AZURE_REDIRECT_PATH'])
def authorized():
    try:
        cache = load_cache()
        result = build_msal_app(cache).acquire_token_by_auth_code_flow(
            session.get("flow", {}),
            request.args,
            scopes=app.config['SCOPE']
        )
        save_cache(cache)

        if "error" in result:
            return render_template("auth_error.html", result=result)

        session["user"] = result.get("id_token_claims")
        return redirect(url_for("index"))
    except ValueError:
        return redirect(url_for("login"))

# Protected routes
@app.route('/')
@login_required
def index():
    return render_template('index.html')

ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def ensure_upload_folder():
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])

def generate_file_hash(file_data):
    return hashlib.sha256(file_data).hexdigest()

def save_user_file(file, user):
    """Save a file for a user with format surname-name-dni.pdf"""
    if not file:
        print("No file provided")
        return None, None
        
    if not file.filename:
        print("Empty filename")
        return None, None
        
    if not allowed_file(file.filename):
        print(f"Invalid file type: {file.filename}")
        return None, None
        
    try:
        # Create filename in format surname-name-dni.ext
        ext = file.filename.rsplit('.', 1)[1].lower()
        
        # Handle both legacy User and UserNew models
        if hasattr(user, 'apellido1'):  # Legacy User model
            surname = user.apellido1
            name = user.nombre
            doc = user.dni_nie
        else:  # UserNew model
            surname = user.last_name
            name = user.name
            doc = user.doc_number
            
        if not all([surname, name, doc]):
            print(f"Missing required user data: surname={surname}, name={name}, doc={doc}")
            return None, None
            
        new_filename = f"{surname.lower()}-{name.lower()}-{doc.lower()}.{ext}"
        new_filename = secure_filename(new_filename)
        
        # Save file
        ensure_upload_folder()
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
        file.save(file_path)
        
        # Generate and return hash for database reference
        with open(file_path, 'rb') as f:
            file_hash = generate_file_hash(f.read())
            
        print(f"File saved successfully: {new_filename} with hash {file_hash}")
        return file_hash, new_filename
    except Exception as e:
        print(f"Error in save_user_file: {str(e)}")
        traceback.print_exc()
        return None, None

def save_project_file(file, project_dir):
    """Save a project file keeping its original name"""
    if file and allowed_file(file.filename):
        try:
            filename = secure_filename(file.filename)
            file_path = os.path.join(project_dir, filename)
            file.save(file_path)
            
            # Generate and return hash for database reference
            with open(file_path, 'rb') as f:
                file_hash = generate_file_hash(f.read())
            return file_hash, filename
        except Exception as e:
            print(f"Error in save_project_file: {str(e)}")
            traceback.print_exc()
            raise
    return None, None

db = SQLAlchemy(app)
socketio = SocketIO(app)

# Custom Array Type for SQLAlchemy
class ArrayType(TypeDecorator):
    impl = Text
    
    def process_bind_param(self, value, dialect):
        if value is not None:
            return json.dumps(value)
        return None
        
    def process_result_value(self, value, dialect):
        if value is not None:
            return json.loads(value)
        return []

# ===============================
# Models (Existing and New)
# ===============================

# --- Original Models ---

class Tecnico(db.Model):
    id = db.Column(db.String(50), primary_key=True, default=lambda: f"TEC_{random.randint(1000, 9999)}")
    nombre = db.Column(db.String(50), unique=True, nullable=False)
    area = db.Column(db.String(50), nullable=False)
    inserciones = db.Column(db.Integer, default=0)

class User(db.Model):
    __tablename__ = 'legacy_users'
    id = db.Column(db.String(50), primary_key=True, default=lambda: f"USR_{random.randint(1000000000, 9999999999)}")
    dni_nie = db.Column(db.String(9), unique=True, nullable=False)
    gesprodi = db.Column(db.String(20), nullable=True)
    nombre = db.Column(db.String(50), nullable=False)
    apellido1 = db.Column(db.String(50), nullable=False)
    apellido2 = db.Column(db.String(50), nullable=True)
    telefono = db.Column(db.String(20), nullable=False)
    colectivo = db.Column(db.String(50), nullable=False)
    acciones = db.Column(db.String(20), nullable=False)
    incidencia = db.Column(db.String(50), nullable=True)
    entidad_asignada = db.Column(db.String(20), nullable=False)
    acceso_programa = db.Column(db.String(2), nullable=False)
    observaciones = db.Column(db.Text, nullable=True)
    sex = db.Column(db.String(1))  # New column for sex
    birth_date = db.Column(db.Date)  # New column for birth date
    projects = db.Column(ArrayType)  # New column for projects array
    files = db.Column(ArrayType)  # New column for file hashes array

    __table_args__ = (
        db.CheckConstraint(
            'sex IN ("M", "F", "O")',  # M = Male, F = Female, O = Other
            name='check_sex'
        ),
        db.CheckConstraint(
            'colectivo IN ("Desemplead@", "Discapacidad", "Mayores", "Exclusión", "Inmigrantes", "Jóvenes sin experiencia laboral", "Mayores de 45")',
            name='check_colectivo'
        ),
        db.CheckConstraint(
            'acciones IN ("Espera", "Citada", "Atendida", "No interesa", "Ocupada", "No acude", "Derivada", "No contesta")',
            name='check_acciones'
        ),
        db.CheckConstraint(
            'incidencia IN ("Ninguna", "Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE")',
            name='check_incidencia'
        ),
        db.CheckConstraint(
            'entidad_asignada IN ("Prodiversa", "Mitad del cielo", "Acompanya", "Forprocer")',
            name='check_entidad'
        ),
        db.CheckConstraint(
            'acceso_programa IN ("Sí", "No")',
            name='check_acceso'
        )
    )

def validate_dni_nie(dni):
    pattern = r'^\d{8}[A-Za-z]$'
    if not re.match(pattern, dni):
        return False, "Formato DNI/NIE inválido. Debe ser 8 dígitos seguidos de una letra (ej: 12345678X)"
    return True, ""

@event.listens_for(User, 'after_insert')
def after_user_insert(mapper, connection, target):
    try:
        from sqlalchemy import text
        
        # First check if the tecnico exists using raw SQL with proper parameter binding
        result = connection.execute(
            text("SELECT id, inserciones FROM tecnico WHERE nombre = :nombre"),
            {"nombre": target.entidad_asignada}
        ).first()
        
        if not result:
            print(f"Error: No tecnico found with nombre={target.entidad_asignada}")
            return
            
        # Update inserciones count using raw SQL with proper parameter binding
        connection.execute(
            text("UPDATE tecnico SET inserciones = :new_count WHERE id = :id"),
            {"new_count": result.inserciones + 1, "id": result.id}
        )
    except Exception as e:
        print(f"Error updating inserciones: {str(e)}")
        traceback.print_exc()

# --- New Models (New Schema) ---

class Employer(db.Model):
    __tablename__ = 'employers'
    employer_id = db.Column(db.String(50), primary_key=True, default=lambda: f"EMP_{random.randint(1000000000, 9999999999)}")
    name = db.Column(db.String)
    last_name = db.Column(db.String)
    second_last_name = db.Column(db.String)
    phone_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    mobile_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    personal_email = db.Column(db.String)
    entity_email = db.Column(db.String)
    address_id = db.Column(db.String(50), db.ForeignKey('address.address_id'))
    username = db.Column(db.String)
    password = db.Column(db.String)
    picture = db.Column(db.LargeBinary)
    active = db.Column(db.Boolean, default=True)
    last_update = db.Column(db.DateTime, default=datetime.utcnow)
    department_id = db.Column(db.String(50), db.ForeignKey('departments.department_id'))
    azure_id = db.Column(db.String(100), unique=True)  # Azure AD Object ID
    azure_email = db.Column(db.String(255))  # Azure AD Email
    azure_display_name = db.Column(db.String(255))  # Azure AD Display Name
    last_login = db.Column(db.DateTime)  # Track last login

class Address(db.Model):
    __tablename__ = 'address'
    address_id = db.Column(db.String(50), primary_key=True, default=lambda: f"ADDR_{random.randint(1000000000, 9999999999)}")
    address = db.Column(db.String)
    address2 = db.Column(db.String)
    postal_code = db.Column(db.String(10))  # Changed to String to handle postal codes with leading zeros
    city_id = db.Column(db.String(50), db.ForeignKey('city.city_id'))

class City(db.Model):
    __tablename__ = 'city'
    city_id = db.Column(db.String(50), primary_key=True, default=lambda: f"CITY_{random.randint(1000000000, 9999999999)}")
    city = db.Column(db.String)
    province_id = db.Column(db.String(50), db.ForeignKey('provinces.province_id'))

class Province(db.Model):
    __tablename__ = 'provinces'
    province_id = db.Column(db.String(50), primary_key=True, default=lambda: f"PROV_{random.randint(1000000000, 9999999999)}")
    province = db.Column(db.String)

class Entity(db.Model):
    __tablename__ = 'entity'
    entity_id = db.Column(db.String(50), primary_key=True, default=lambda: f"ENTI_{random.randint(1000000000, 9999999999)}")
    name = db.Column(db.String)

class Department(db.Model):
    __tablename__ = 'departments'
    department_id = db.Column(db.String(50), primary_key=True, default=lambda: f"DEPT_{random.randint(1000000000, 9999999999)}")
    name = db.Column(db.String)
    entity_id = db.Column(db.String(50), db.ForeignKey('entity.entity_id'))

# Note: To avoid name conflicts, we name this model "UserNew"
class UserNew(db.Model):
    __tablename__ = 'users'
    user_no = db.Column(db.String(50), primary_key=True, default=lambda: f"USR_{random.randint(1000000000, 9999999999)}")
    doc_type_id = db.Column(db.String(50), db.ForeignKey('id_docs.doc_type_id'))
    doc_number = db.Column(db.String)
    name = db.Column(db.String)
    last_name = db.Column(db.String)
    second_last_name = db.Column(db.String)
    phone_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    mobile_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    email = db.Column(db.String)
    technician_id = db.Column(db.String(50))  # Not linked as foreign key because of type mismatch
    social_group_id = db.Column(db.String(50), db.ForeignKey('social_groups.social_group_id'))
    address_id = db.Column(db.String(50), db.ForeignKey('address.address_id'))
    entity_id = db.Column(db.String(50), db.ForeignKey('entity.entity_id'))
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)
    users_info_id = db.Column(db.String(50), db.ForeignKey('users_info.users_info_id'))
    actions = db.Column(db.String(20))  # New column for actions
    incident = db.Column(db.String(50))  # New column for incidents
    sex = db.Column(db.String(1))  # New column for sex
    birth_date = db.Column(db.Date)  # New column for birth date
    projects = db.Column(ArrayType)  # New column for projects array
    files = db.Column(ArrayType)  # New column for file hashes array

    __table_args__ = (
        db.CheckConstraint(
            'actions IN ("Espera", "Citada", "Atendida", "No interesa", "Ocupada", "No acude", "Derivada", "No contesta")',
            name='check_actions'
        ),
        db.CheckConstraint(
            'incident IN ("Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE")',
            name='check_incident'
        ),
        db.CheckConstraint(
            'sex IN ("M", "F", "O")',  # M = Male, F = Female, O = Other
            name='check_sex'
        )
    )

class IdDoc(db.Model):
    __tablename__ = 'id_docs'
    doc_type_id = db.Column(db.String(50), primary_key=True, default=lambda: f"DOC_{random.randint(1000000000, 9999999999)}")
    doc_name = db.Column(db.String(100))
    doc_template = db.Column(db.CHAR)
    doc_type_di = db.Column(db.String(64))  # Hash for DNI/NIF file
    cert_extr_id = db.Column(db.String(64))  # Hash for Certificado extranjería file
    vida_laboral_id = db.Column(db.String(64))  # Hash for Vida laboral file

class SocialGroup(db.Model):
    __tablename__ = 'social_groups'
    social_group_id = db.Column(db.String(50), primary_key=True, default=lambda: f"SITU_{random.randint(1000000000, 9999999999)}")
    group_name = db.Column(db.String)

class UsersInfo(db.Model):
    __tablename__ = 'users_info'
    users_info_id = db.Column(db.String(50), primary_key=True, default=lambda: f"INFO_{random.randint(1000000000, 9999999999)}")
    technician_observ = db.Column(db.String)

class Project(db.Model):
    __tablename__ = 'projects'
    id = db.Column(db.String(50), primary_key=True, default=lambda: f"PROY_{random.randint(1000000000, 9999999999)}")
    name = db.Column(db.String, nullable=False)
    date_started = db.Column(db.Date, nullable=True)
    date_finished = db.Column(db.Date, nullable=True)
    number_users = db.Column(db.Integer, default=0)
    number_tecnicos = db.Column(db.Integer, default=0)
    files = db.Column(ArrayType, nullable=True)  # Array of {hash: string, name: string}
    active = db.Column(db.Boolean, default=True)

class Itinerario(db.Model):
    __tablename__ = 'itinerario'
    dni = db.Column(db.String(9), db.ForeignKey('legacy_users.dni_nie'), primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    second_last_name = db.Column(db.String(50), nullable=True)
    job_status = db.Column(db.String(50), nullable=True)
    priority_sector = db.Column(db.String(100), nullable=True)
    priority_sector2 = db.Column(db.String(100), nullable=True)
    cv = db.Column(db.String(64), nullable=True)  # Hash for CV file
    education = db.Column(db.String(100), nullable=True)
    integrales_course = db.Column(ArrayType, nullable=True)
    insercion_date = db.Column(db.Date, nullable=True)
    contract_type = db.Column(db.String(50), nullable=True)
    workday_percent = db.Column(db.Float, nullable=True)
    insercion_date2 = db.Column(db.Date, nullable=True)
    contract_type2 = db.Column(db.String(50), nullable=True)
    workday_percent2 = db.Column(db.Float, nullable=True)

    # Relationship with User model
    user = db.relationship('User', backref=db.backref('itinerario', lazy=True))

# ===============================
# Routes (Template Views)
# ===============================

@app.route('/projects')
def projects():
    projects = Project.query.all()
    return render_template('projects.html', projects=projects)

@app.route('/project/<project_id>')
def project_detail(project_id):
    project = Project.query.get_or_404(project_id)
    
    # Get users in this project by joining with User and filtering by project_id
    users = db.session.query(Itinerario)\
        .join(User, Itinerario.dni == User.dni_nie)\
        .filter(User.projects.contains(project_id))\
        .all()
    
    # Get users not in this project for the add user modal
    users_in_project = User.query.filter(User.projects.contains(project_id)).all()
    user_ids_in_project = [user.dni_nie for user in users_in_project]
    available_users = User.query.filter(~User.dni_nie.in_(user_ids_in_project) if user_ids_in_project else True).all()
    
    return render_template('project_detail.html', 
                         project=project, 
                         users=users, 
                         available_users=available_users)

@app.route('/add_users_to_project', methods=['POST'])
def add_users_to_project():
    try:
        project_id = request.form.get('project_id')
        selected_users = json.loads(request.form.get('users', '[]'))
        
        if not project_id or not selected_users:
            return jsonify({"error": "Missing required data"}), 400

        project = Project.query.get_or_404(project_id)
        
        for user_dni in selected_users:
            # Get user data
            user = User.query.filter_by(dni_nie=user_dni).first()
            if not user:
                continue

            # Create itinerario record
            itinerario = Itinerario(
                dni=user_dni,
                name=user.nombre,
                last_name=user.apellido1,
                second_last_name=user.apellido2,
                job_status=request.form.get('job_status'),
                priority_sector=request.form.get('priority_sector'),
                priority_sector2=request.form.get('priority_sector2'),
                education=request.form.get('education'),
                insercion_date=datetime.strptime(request.form.get('insercion_date'), '%Y-%m-%d').date() if request.form.get('insercion_date') else None,
                contract_type=request.form.get('contract_type'),
                workday_percent=float(request.form.get('workday_percent')) if request.form.get('workday_percent') else None,
                insercion_date2=datetime.strptime(request.form.get('insercion_date2'), '%Y-%m-%d').date() if request.form.get('insercion_date2') else None,
                contract_type2=request.form.get('contract_type2'),
                workday_percent2=float(request.form.get('workday_percent2')) if request.form.get('workday_percent2') else None
            )

            # Handle CV file
            if 'cv' in request.files:
                cv_file = request.files['cv']
                if cv_file and allowed_file(cv_file.filename):
                    # Create user directory in project
                    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'files', project_id, user_dni)
                    os.makedirs(user_dir, exist_ok=True)
                    
                    # Save file with user-specific name
                    file_hash, _ = save_user_file(cv_file, user)
                    if file_hash:
                        itinerario.cv = file_hash

            db.session.add(itinerario)
            
            # Update user's projects array
            if not user.projects:
                user.projects = []
            user.projects.append(project_id)
            
            # Update project's user count
            project.number_users += 1

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        print(f"Error in add_users_to_project: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/get_user_profile/<user_id>')
def get_user_profile(user_id):
    user = Itinerario.query.get_or_404(user_id)
    return jsonify({
        'success': True,
        'html': render_template('_user_profile.html', user=user)
    })

@app.route('/add_user_itinerario/<user_id>', methods=['POST'])
def add_user_itinerario(user_id):
    try:
        # Get the user
        user = User.query.filter_by(dni_nie=user_id).first_or_404()
        
        # Create new itinerario
        itinerario = Itinerario(
            dni=user_id,
            name=user.nombre,
            last_name=user.apellido1,
            second_last_name=user.apellido2,
            job_status=request.form.get('job_status'),
            education=request.form.get('education'),
            priority_sector=request.form.get('priority_sector')
        )

        # Handle CV file
        if 'cv' in request.files:
            cv_file = request.files['cv']
            if cv_file and allowed_file(cv_file.filename):
                file_hash, _ = save_user_file(cv_file, user)
                if file_hash:
                    itinerario.cv = file_hash

        db.session.add(itinerario)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        print(f"Error creating itinerario: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/edit_user/<user_id>')
def edit_user(user_id):
    user = User.query.filter_by(dni_nie=user_id).first_or_404()
    return render_template('edit_user.html', user=user)

@app.route('/user_profile/<user_id>')
def user_profile(user_id):
    user = User.query.filter_by(dni_nie=user_id).first_or_404()
    itinerario = Itinerario.query.get(user_id)
    return render_template('user_profile.html', 
                         user=user, 
                         itinerario=itinerario,
                         Project=Project)  # Pass the Project model to the template

@app.route('/update_user_itinerario/<user_id>', methods=['POST'])
def update_user_itinerario(user_id):
    try:
        user = Itinerario.query.get_or_404(user_id)

        # Update basic fields
        user.job_status = request.form.get('job_status')
        user.education = request.form.get('education')
        user.priority_sector = request.form.get('priority_sector')
        user.priority_sector2 = request.form.get('priority_sector2')
        
        # Handle dates
        if request.form.get('insercion_date'):
            user.insercion_date = datetime.strptime(request.form.get('insercion_date'), '%Y-%m-%d').date()
        if request.form.get('insercion_date2'):
            user.insercion_date2 = datetime.strptime(request.form.get('insercion_date2'), '%Y-%m-%d').date()
        
        # Update contract information
        user.contract_type = request.form.get('contract_type')
        user.contract_type2 = request.form.get('contract_type2')
        
        # Update workday percentages
        if request.form.get('workday_percent'):
            user.workday_percent = float(request.form.get('workday_percent'))
        if request.form.get('workday_percent2'):
            user.workday_percent2 = float(request.form.get('workday_percent2'))

            # Handle CV file
            if 'cv' in request.files:
                cv_file = request.files['cv']
                file_hash, _ = save_user_file(cv_file, user)
                if file_hash:
                    user.cv = file_hash

        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        print(f"Error updating user itinerario: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/new_project')
def new_project():
    return render_template('new_project.html')

@app.route('/add_project', methods=['POST'])
def add_project():
    try:
        # Generate project ID (PROY_XXXX)
        project_count = Project.query.count()
        project_id = f"PROY_{(project_count + 1):04d}"

        # Create project directory for files
        project_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'files', project_id, 'docs')
        os.makedirs(project_dir, exist_ok=True)

        # Process project files keeping original names
        files = request.files.getlist('files')
        file_data = []
        
        for file in files:
            file_hash, filename = save_project_file(file, project_dir)
            if file_hash and filename:
                file_data.append({'hash': file_hash, 'name': filename})

        # Create new project
        new_project = Project(
            id=project_id,
            name=request.form.get('name'),
            date_started=datetime.strptime(request.form.get('date_started'), '%Y-%m-%d').date() if request.form.get('date_started') else None,
            date_finished=datetime.strptime(request.form.get('date_finished'), '%Y-%m-%d').date() if request.form.get('date_finished') else None,
            number_users=0,
            number_tecnicos=0,
            files=file_data,
            active=True
        )
        
        db.session.add(new_project)
        db.session.commit()
        
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        print(f"Error in add_project: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/usuarios')
def usuarios():
    return render_template('usuarios.html')

@app.route('/tecnicos')
def tecnicos():
    return render_template('tecnicos.html')

@app.route('/employers')
@login_required
def employers():
    return render_template('employers.html')

@app.route('/addresses')
def addresses():
    return render_template('addresses.html')

@app.route('/cities')
def cities():
    return render_template('cities.html')

@app.route('/provinces')
def provinces():
    return render_template('provinces.html')

@app.route('/entities')
def entities():
    return render_template('entities.html')

@app.route('/departments')
def departments():
    return render_template('departments.html')

@app.route('/new_users')
def new_users():
    # Ensure we have at least one record in each required table for testing
    try:
        # Create test IdDoc if it doesn't exist
        if not IdDoc.query.first():
            test_doc = IdDoc(
                doc_type_id='DOC_' + str(random.randint(1000, 9999)),
                doc_name='Test Document',
                doc_template='T'
            )
            db.session.add(test_doc)
            db.session.commit()
            print("Created test IdDoc record")

        # Create test SocialGroup if it doesn't exist
        if not SocialGroup.query.first():
            test_group = SocialGroup(
                social_group_id='SITU_' + str(random.randint(1000, 9999)),
                group_name='Test Group'
            )
            db.session.add(test_group)
            db.session.commit()
            print("Created test SocialGroup record")

        # Create test Entity if it doesn't exist
        if not Entity.query.first():
            test_entity = Entity(
                entity_id='ENTI_TEST_' + str(random.randint(1000, 9999)),
                name='Test Entity'
            )
            db.session.add(test_entity)
            db.session.commit()
            print("Created test Entity record")

    except Exception as e:
        db.session.rollback()
        print(f"Error creating test records: {str(e)}")
        traceback.print_exc()
    
    return render_template('new_users.html')

@app.route('/id_docs')
def id_docs():
    return render_template('id_docs.html')

@app.route('/social_groups')
def social_groups():
    return render_template('social_groups.html')

@app.route('/user_info')
def user_info():
    return render_template('user_info.html')

# ===============================
# AJAX Endpoints for Data Operations
# ===============================

# --- Original Users ---
@app.route('/add_user', methods=['POST'])
def add_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        dni_validation, dni_error = validate_dni_nie(data.get('dni_nie', ''))
        if not dni_validation:
            return jsonify({"error": dni_error}), 400

        dni_normalized = data['dni_nie'].upper()
        if User.query.filter_by(dni_nie=dni_normalized).first():
            return jsonify({"error": "DNI/NIE ya existe en la base de datos"}), 400

        required_fields = ['nombre', 'apellido1', 'telefono', 'colectivo', 'acciones', 'entidad_asignada', 'acceso_programa']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Campo requerido faltante: {field}"}), 400

        validations = {
            'colectivo': ["Desemplead@", "Discapacidad", "Mayores", "Exclusión", "Inmigrantes", "Jóvenes sin experiencia laboral", "Mayores de 45"],
            'acciones': ["Espera", "Citada", "Atendida", "No interesa", "Ocupada", "No acude", "Derivada", "No contesta"],
            'entidad_asignada': ["Prodiversa", "Mitad del cielo", "Acompanya", "Forprocer"],
            'acceso_programa': ["Sí", "No"]
        }

        for field, allowed in validations.items():
            if data[field] not in allowed:
                return jsonify({"error": f"Valor inválido para {field}"}), 400

        if data.get('incidencia') and data['incidencia'] not in ["Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE"]:
            return jsonify({"error": "Valor de incidencia inválido"}), 400

        new_user = User(
            dni_nie=dni_normalized,
            gesprodi=data.get('gesprodi'),
            nombre=data['nombre'],
            apellido1=data['apellido1'],
            apellido2=data.get('apellido2'),
            telefono=data['telefono'],
            colectivo=data['colectivo'],
            acciones=data['acciones'],
            incidencia=data.get('incidencia') or None,
            entidad_asignada=data['entidad_asignada'],
            acceso_programa=data['acceso_programa'],
            observaciones=data.get('observaciones')
        )
        db.session.add(new_user)
        db.session.commit()
        socketio.emit('update', {'message': 'new user added'})
        return jsonify(success=True)
    except IntegrityError as e:
        db.session.rollback()
        if "dni_nie" in str(e).lower():
            return jsonify({"error": "DNI/NIE ya existe en la base de datos"}), 400
        return jsonify({"error": "Error de integridad de datos"}), 400
    except Exception as e:
        db.session.rollback()
        print(f"Error in add_new_user: {str(e)}")  # Debug log
        import traceback
        traceback.print_exc()  # Print full stack trace
        return jsonify({"error": str(e)}), 500

@app.route('/update_user', methods=['POST'])
def update_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        
        # Find user by DNI/NIE
        user = User.query.filter_by(dni_nie=data['dni_nie']).first()
        if not user:
            return jsonify({"error": "Usuario no encontrado"}), 404

        # Update user fields
        user.nombre = data['nombre']
        user.apellido1 = data['apellido1']
        user.apellido2 = data.get('apellido2')
        user.telefono = data['telefono']
        user.colectivo = data['colectivo']
        user.acciones = data['acciones']
        user.incidencia = data.get('incidencia')
        user.entidad_asignada = data['entidad_asignada']
        user.acceso_programa = data['acceso_programa']
        user.observaciones = data.get('observaciones')

        # Also update the UserNew record if it exists
        user_new = UserNew.query.filter_by(doc_number=data['dni_nie']).first()
        if user_new:
            user_new.name = data['nombre']
            user_new.last_name = data['apellido1']
            user_new.second_last_name = data.get('apellido2')
            user_new.phone_number = data['telefono']
            user_new.actions = data['acciones']
            user_new.incident = data.get('incidencia')

        db.session.commit()
        socketio.emit('update', {'message': 'user updated'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_users')
def get_users():
    users = User.query.all()
    return jsonify([{
        'dni_nie': user.dni_nie,
        'nombre': user.nombre,
        'apellido1': user.apellido1,
        'apellido2': user.apellido2,
        'telefono': user.telefono,
        'colectivo': user.colectivo,
        'acciones': user.acciones,
        'incidencia': user.incidencia,
        'entidad_asignada': user.entidad_asignada,
        'acceso_programa': user.acceso_programa,
        'observaciones': user.observaciones,
        'sex': user.sex,
        'birth_date': user.birth_date.strftime('%Y-%m-%d') if user.birth_date else None,
        'projects': user.projects if user.projects else [],
        'files': user.files if user.files else []
    } for user in users])

# --- Tecnicos ---
@app.route('/add_tecnico', methods=['POST'])
def add_tecnico():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_tecnico = Tecnico(
            id=f"TEC_{data['nombre'][:2].upper()}_{random.randint(1000, 9999)}",
            nombre=data['nombre'],
            area=data['area']
        )
        db.session.add(new_tecnico)
        db.session.commit()
        socketio.emit('update', {'message': 'new tecnico added'})
        return jsonify(success=True)
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "El nombre de la entidad debe ser único"}), 400
    except Exception as e:
        db.session.rollback()
        print(f"Error in add_new_user: {str(e)}")  # Debug log
        import traceback
        traceback.print_exc()  # Print full stack trace
        return jsonify({"error": str(e)}), 500

@app.route('/get_tecnicos')
def get_tecnicos():
    tecnicos = Tecnico.query.all()
    return jsonify([{
        'id': t.id,
        'nombre': t.nombre,
        'area': t.area,
        'inserciones': t.inserciones
    } for t in tecnicos])

# --- Employers ---
@app.route('/add_employer', methods=['POST'])
def add_employer():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_employer = Employer(
            employer_id=data.get('employer_id'),
            name=data.get('name'),
            last_name=data.get('last_name'),
            second_last_name=data.get('second_last_name'),
            phone_number=data.get('phone_number'),
            mobile_number=data.get('mobile_number'),
            personal_email=data.get('personal_email'),
            entity_email=data.get('entity_email'),
            address_id=data.get('address_id'),
            username=data.get('username'),
            password=data.get('password'),
            picture=data.get('picture'),
            active=data.get('active', True),
            last_update=data.get('last_update'),
            department_id=data.get('department_id'),
            azure_id=data.get('azure_id'),
            azure_email=data.get('azure_email'),
            azure_display_name=data.get('azure_display_name'),
            last_login=data.get('last_login')
        )
        db.session.add(new_employer)
        db.session.commit()
        socketio.emit('update', {'message': 'new employer added'})
        return jsonify(success=True)
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Integrity error in employer"}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_employers')
def get_employers():
    employers = Employer.query.all()
    return jsonify([{
        'id': emp.employer_id,
        'name': f"{emp.name} {emp.last_name}"
    } for emp in employers])


@app.route('/get_social_groups_list')
def get_social_groups_list():
    groups = SocialGroup.query.all()
    return jsonify([{
        'id': g.social_group_id,
        'name': g.group_name
    } for g in groups])

@app.route('/get_entities_list')
def get_entities_list():
    entities = Entity.query.all()
    return jsonify([{
        'id': e.entity_id,
        'name': e.name
    } for e in entities])

# --- Addresses ---
@app.route('/add_address', methods=['POST'])
def add_address():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_address = Address(
            address_id=data.get('address_id'),
            address=data.get('address'),
            address2=data.get('address2'),
            postal_code=data.get('postal_code'),
            city_id=data.get('city_id')
        )
        db.session.add(new_address)
        db.session.commit()
        socketio.emit('update', {'message': 'new address added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_addresses')
def get_addresses():
    addresses = Address.query.all()
    return jsonify([{
        'address_id': addr.address_id,
        'address': addr.address,
        'address2': addr.address2,
        'postal_code': addr.postal_code,
        'city_id': addr.city_id
    } for addr in addresses])

# --- Cities ---
@app.route('/add_city', methods=['POST'])
def add_city():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_city = City(
            city_id=data.get('city_id'),
            city=data.get('city'),
            province_id=data.get('province_id')
        )
        db.session.add(new_city)
        db.session.commit()
        socketio.emit('update', {'message': 'new city added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_cities_list')
def get_cities_list():
    cities = City.query.all()
    return jsonify([{
        'id': c.city_id,
        'name': c.city,
        'province_id': c.province_id
    } for c in cities])

@app.route('/get_provinces_list')
def get_provinces_list():
    provinces = Province.query.all()
    return jsonify([{
        'id': p.province_id,
        'name': p.province
    } for p in provinces])

# --- Provinces ---
@app.route('/add_province', methods=['POST'])
def add_province():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_province = Province(
            province_id=data.get('province_id'),
            province=data.get('province')
        )
        db.session.add(new_province)
        db.session.commit()
        socketio.emit('update', {'message': 'new province added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_provinces')
def get_provinces():
    provinces = Province.query.all()
    return jsonify([{
        'province_id': p.province_id,
        'province': p.province
    } for p in provinces])

# --- Entities ---
@app.route('/add_entity', methods=['POST'])
def add_entity():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_entity = Entity(
            entity_id=data.get('entity_id'),
            name=data.get('name')
        )
        db.session.add(new_entity)
        db.session.commit()
        socketio.emit('update', {'message': 'new entity added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_entities')
def get_entities():
    entities = Entity.query.all()
    return jsonify([{
        'entity_id': e.entity_id,
        'name': e.name
    } for e in entities])

# --- Departments ---
@app.route('/add_department', methods=['POST'])
def add_department():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_department = Department(
            department_id=data.get('department_id'),
            name=data.get('name'),
            entity_id=data.get('entity_id')
        )
        db.session.add(new_department)
        db.session.commit()
        socketio.emit('update', {'message': 'new department added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_departments')
def get_departments():
    departments = Department.query.all()
    return jsonify([{
        'department_id': d.department_id,
        'name': d.name,
        'entity_id': d.entity_id
    } for d in departments])

# --- New Users (UserNew) ---
def generate_address_id():
    import random
    return f"ADDR_{random.randint(1000000000, 9999999999)}"

@app.route('/add_new_user', methods=['POST'])
def add_new_user():
    try:
        print("Form data:", request.form)  # Debug log
        print("Files:", request.files)  # Debug log
        
        # Generate address ID and create address record
        address_text = request.form.get('address')
        postal_code = request.form.get('postal_code')
        city_id = request.form.get('city_id')
        
        if address_text or postal_code or city_id:
            address_id = generate_address_id()
            new_address = Address(
                address_id=address_id,
                address=address_text,
                postal_code=postal_code,
                city_id=city_id
            )
            db.session.add(new_address)
            db.session.commit()
        else:
            address_id = None

        # Convert form data to appropriate types
        doc_number = request.form.get('doc_number')
        phone_number = request.form.get('phone_number')
        mobile_number = request.form.get('mobile_number')
        technician_id = request.form.get('technician_id')
        social_group_id = request.form.get('social_group_id')
        entity_id = request.form.get('entity_id')
        
        # Get incident value and handle it according to the model's constraints
        incident = request.form.get('incident')
        if incident == "Ninguna":
            incident = None
        
        # Generate user_no
        user_no = f"USR_{random.randint(1000, 9999)}"
        
        # Validate required IDs exist
        doc_type_id = request.form.get('doc_type_id')
        if not IdDoc.query.get(doc_type_id):
            return jsonify({"error": "Invalid document type"}), 400

        entity_id = request.form.get('entity_id')
        if not Entity.query.get(entity_id):
            return jsonify({"error": "Invalid entity"}), 400

        if request.form.get('social_group_id'):
            if not SocialGroup.query.get(request.form.get('social_group_id')):
                return jsonify({"error": "Invalid social group"}), 400

        if request.form.get('city_id'):
            if not City.query.get(request.form.get('city_id')):
                return jsonify({"error": "Invalid city"}), 400

        # Create UserNew record
        new_user = UserNew(
            user_no=user_no,
            doc_type_id=doc_type_id,
            doc_number=doc_number,
            name=request.form.get('name'),
            last_name=request.form.get('last_name'),
            second_last_name=request.form.get('second_last_name'),
            phone_number=phone_number,
            mobile_number=mobile_number,
            email=request.form.get('email'),
            technician_id=technician_id,
            social_group_id=social_group_id,
            address_id=address_id if address_id else None,
            entity_id=entity_id,
            create_date=datetime.utcnow(),
            active=True,
            actions=request.form.get('actions'),
            incident=incident,
            sex=request.form.get('sex'),
            birth_date=datetime.strptime(request.form.get('birth_date'), '%Y-%m-%d').date() if request.form.get('birth_date') else None,
            projects=[],
            files=[]
        )
        db.session.add(new_user)
        db.session.commit()

        # Process and save files
        file_hashes = {}
        files_array = []  # Array to store file hashes
        
        print("Processing files...")  # Debug log
        if 'dni_file' in request.files:
            print("Processing DNI file...")  # Debug log
            try:
                file_hash, _ = save_user_file(request.files['dni_file'], new_user)
                print(f"DNI file hash: {file_hash}")  # Debug log
                if file_hash:
                    file_hashes['doc_type_di'] = file_hash
                    files_array.append(file_hash)
            except Exception as e:
                print(f"Error saving DNI file: {str(e)}")  # Debug log
                traceback.print_exc()
        
        if 'cert_extr_file' in request.files:
            print("Processing cert_extr file...")  # Debug log
            try:
                file_hash, _ = save_user_file(request.files['cert_extr_file'], new_user)
                print(f"cert_extr file hash: {file_hash}")  # Debug log
                if file_hash:
                    file_hashes['cert_extr_id'] = file_hash
                    files_array.append(file_hash)
            except Exception as e:
                print(f"Error saving cert_extr file: {str(e)}")  # Debug log
                traceback.print_exc()
        
        if 'vida_laboral_file' in request.files:
            print("Processing vida_laboral file...")  # Debug log
            try:
                file_hash, _ = save_user_file(request.files['vida_laboral_file'], new_user)
                print(f"vida_laboral file hash: {file_hash}")  # Debug log
                if file_hash:
                    file_hashes['vida_laboral_id'] = file_hash
                    files_array.append(file_hash)
            except Exception as e:
                print(f"Error saving vida_laboral file: {str(e)}")  # Debug log
                traceback.print_exc()

        # Update id_docs with file hashes if any files were uploaded
        if file_hashes:
            id_doc = IdDoc.query.get(new_user.doc_type_id)
            if id_doc:
                for field, hash_value in file_hashes.items():
                    setattr(id_doc, field, hash_value)
                db.session.commit()

        # Update the files array in UserNew
        new_user.files = files_array
        db.session.commit()

        # Also create a record in the legacy User table
        legacy_user = User(
            dni_nie=new_user.doc_number,
            nombre=new_user.name,
            apellido1=new_user.last_name,
            apellido2=new_user.second_last_name,
            telefono=new_user.phone_number,
            # Map values from new user form
            colectivo="Desemplead@",
            acciones=new_user.actions,
            incidencia=new_user.incident,
            entidad_asignada="Prodiversa",
            acceso_programa="Sí",
            sex=request.form.get('sex'),
            birth_date=datetime.strptime(request.form.get('birth_date'), '%Y-%m-%d').date() if request.form.get('birth_date') else None,
            projects=request.form.getlist('projects[]') if request.form.getlist('projects[]') else [],
            files=list(file_hashes.values()) if file_hashes else []
        )

        # Validate incidencia for legacy_user
        valid_incidencia_values = ["Ninguna", "Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE"]
        if legacy_user.incidencia not in valid_incidencia_values and legacy_user.incidencia is not None:
            return jsonify({"error": "Valor de incidencia inválido para legacy_user"}), 400

        db.session.add(legacy_user)
        db.session.commit()

        # Emit separate events for each table update
        socketio.emit('update', {'message': 'new user added', 'table': 'new_users'})
        socketio.emit('update', {'message': 'new user added', 'table': 'users'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_new_users')
def get_new_users():
    # Join with Entity to get entity name
    users_new = db.session.query(UserNew, Entity.name.label('entity_name'))\
        .outerjoin(Entity, UserNew.entity_id == Entity.entity_id)\
        .all()
    
    return jsonify([{
        'name': u[0].name,
        'last_name': u[0].last_name,
        'create_date': u[0].create_date,
        'entity': u[1] if u[1] else ''  # Use entity name from join
    } for u in users_new])

# --- ID Docs ---
@app.route('/add_id_doc', methods=['POST'])
def add_id_doc():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_id_doc = IdDoc(
            doc_type_id=data.get('doc_type_id'),
            doc_name=data.get('doc_name'),
            doc_template=data.get('doc_template')
        )
        db.session.add(new_id_doc)
        db.session.commit()
        socketio.emit('update', {'message': 'new id_doc added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_id_docs')
def get_id_docs():
    docs = IdDoc.query.all()
    return jsonify([{
        'doc_type_id': d.doc_type_id,
        'doc_name': d.doc_name,
        'doc_template': d.doc_template,
        'doc_type_di': d.doc_type_di,
        'cert_extr_id': d.cert_extr_id,
        'vida_laboral_id': d.vida_laboral_id
    } for d in docs])

# --- Social Groups ---
@app.route('/add_social_group', methods=['POST'])
def add_social_group():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_social_group = SocialGroup(
            social_group_id=data.get('social_group_id'),
            group_name=data.get('group_name')
        )
        db.session.add(new_social_group)
        db.session.commit()
        socketio.emit('update', {'message': 'new social group added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_social_groups')
def get_social_groups():
    groups = SocialGroup.query.all()
    return jsonify([{
        'social_group_id': g.social_group_id,
        'group_name': g.group_name
    } for g in groups])

# --- Users Info ---
@app.route('/add_user_info', methods=['POST'])
def add_user_info():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_user_info = UsersInfo(
            users_info_id=data.get('users_info_id'),
            technician_observ=data.get('technician_observ')
        )
        db.session.add(new_user_info)
        db.session.commit()
        socketio.emit('update', {'message': 'new user info added'})
        return jsonify(success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_user_info')
def get_user_info():
    infos = UsersInfo.query.all()
    return jsonify([{
        'users_info_id': info.users_info_id,
        'technician_observ': info.technician_observ
    } for info in infos])

@app.route('/download_file/<file_hash>')
def download_file(file_hash):
    # Search for the file in the bin directory and its subdirectories
    for root, dirs, files in os.walk(app.config['UPLOAD_FOLDER']):
        for file in files:
            if file.startswith(file_hash) or generate_file_hash(open(os.path.join(root, file), 'rb').read()) == file_hash:
                file_path = os.path.join(root, file)
                # Get the original filename from the path
                filename = os.path.basename(file_path)
                return send_file(file_path, as_attachment=True, download_name=filename)
    return jsonify({"error": "File not found"}), 404

@app.route('/edit_itinerario/<user_id>')
def edit_itinerario(user_id):
    user = User.query.filter_by(dni_nie=user_id).first_or_404()
    itinerario = Itinerario.query.get_or_404(user_id)
    return render_template('edit_itinerario.html', user=user, itinerario=itinerario)

# ===============================
# SocketIO Connection
# ===============================
@socketio.on('connect')
def handle_connect():
    emit('update', {'message': 'connected'})

# ===============================
# Run the Application
# ===============================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # This will create all tables if they don't exist
        
        # Add entities and their corresponding tecnicos if they don't exist
        entities_and_areas = {
            'Prodiversa': 'Área Social',
            'Mitad del cielo': 'Área Social',
            'Acompanya': 'Área Social',
            'Forprocer': 'Área Social'
        }
        for name, area in entities_and_areas.items():
            # Add entity if it doesn't exist
            if not Entity.query.filter_by(name=name).first():
                entity = Entity(
                    entity_id='ENTI_' + ''.join(word[0].upper() for word in name.split())[:2] + str(random.randint(1000, 9999)),
                    name=name
                )
                db.session.add(entity)
                db.session.commit()
            
            # Add tecnico if it doesn't exist
            if not Tecnico.query.filter_by(nombre=name).first():
                tecnico = Tecnico(
                    id=f"TEC_{name[:2].upper()}_{random.randint(1000, 9999)}",
                    nombre=name,
                    area=area
                )
                db.session.add(tecnico)
                db.session.commit()
        
        # Add social groups if they don't exist
        social_groups = [
            'Inactivo',
            'Discapacidad',
            'Mayores',
            'Exclusión',
            'Inmigrante',
            'Joven sin experiencia laboral',
            'Mayores de 45'
        ]
        for group_name in social_groups:
            if not SocialGroup.query.filter_by(group_name=group_name).first():
                group_id = 'SITU_' + str(random.randint(1000, 9999))
                group = SocialGroup(social_group_id=group_id, group_name=group_name)
                db.session.add(group)
        
        try:
            db.session.commit()
        except Exception as e:
            print(f"Error adding default data: {str(e)}")
            db.session.rollback()
    socketio.run(app, host='localhost', port=5050)
