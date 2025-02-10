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
import requests
from datetime import datetime
from werkzeug.utils import secure_filename
import base64

# Custom filter for base64 encoding
def b64encode_filter(data):
    if data is None:
        return ''
    return base64.b64encode(data).decode('utf-8')

# Custom ArrayType for SQLite
class ArrayType(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return json.dumps(value)
        return None

    def process_result_value(self, value, dialect):
        if value is not None:
            return json.loads(value)
        return []

app = Flask(__name__)

# Register the base64 filter
app.jinja_env.filters['b64encode'] = b64encode_filter

# Azure AD Configuration
app.config.update({
    'SESSION_TYPE': 'filesystem',
    'AZURE_CLIENT_ID': '434df998-aa76-49b1-b92e-0f9a738e5b6c',
    'AZURE_CLIENT_SECRET': 'YJe8Q~-Qfh_-ZeB3-O4VUulBgX-~fzJlsPBlwaYG',
    'AZURE_TENANT_ID': 'f13127dc-6782-4765-bb5b-f47085a7ff8f',
    'AZURE_AUTHORITY': 'https://login.microsoftonline.com/f13127dc-6782-4765-bb5b-f47085a7ff8f',
    'AZURE_REDIRECT_PATH': '/getAToken',
    'SCOPE': [
        'https://graph.microsoft.com/User.Read',
        'https://graph.microsoft.com/User.Read.All',
        'https://graph.microsoft.com/email',
        'https://graph.microsoft.com/profile'
    ],
    'ENDPOINT': 'https://graph.microsoft.com/v1.0/me'
})

# Other app configurations
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'bin'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

Session(app)  # Initialize Flask-Session
db = SQLAlchemy(app)
socketio = SocketIO(app)

# Helper functions for Azure AD and Graph API
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

def get_user_profile_from_graph(access_token):
    """Get user profile from Microsoft Graph using access token"""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    response = requests.get(app.config['ENDPOINT'], headers=headers)
    if response.status_code == 200:
        return response.json()
    return None

def get_user_photo_from_graph(access_token):
    """Get user photo from Microsoft Graph using access token"""
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    photo_endpoint = 'https://graph.microsoft.com/v1.0/me/photo/$value'
    response = requests.get(photo_endpoint, headers=headers)
    if response.status_code == 200:
        return response.content
    return None

def generate_tecnico_id(name, surname):
    """Generate a unique tecnico ID based on name and surname"""
    base = f"TEC_{name[0].upper()}{surname[0].upper()}"
    random_num = random.randint(1000, 9999)
    return f"{base}_{random_num}"

# Decorator definition
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("show_login"))
        return f(*args, **kwargs)
    return decorated_function

# Authentication routes
@app.route("/")
@login_required
def index():
    return render_template('index.html')

@app.route("/login_page")
def show_login():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/login")
def login():
    # Generate state if not present
    if not session.get("state"):
        session["state"] = str(uuid.uuid4())
    
    # Generate and store auth flow
    auth_flow = build_msal_app().initiate_auth_code_flow(
        app.config['SCOPE'],
        redirect_uri=url_for("authorized", _external=True)
    )
    session["flow"] = auth_flow
    
    return redirect(auth_flow['auth_uri'])

@app.route("/getAToken")
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
        
        # Get user profile from Microsoft Graph
        graph_data = get_user_profile_from_graph(result['access_token'])
        if not graph_data:
            return render_template("auth_error.html", result={"error": "Failed to get user profile from Graph API"})
        
        # Check if tecnico exists
        tecnico = Tecnico.query.filter_by(azure_id=session["user"]["oid"]).first()
        
        if not tecnico:
            # Get name and surname with fallbacks
            name = graph_data.get('givenName')
            surname = graph_data.get('surname')
            
            # If name or surname is missing, try to extract from displayName
            if not name or not surname:
                display_name = graph_data.get('displayName', '')
                name_parts = display_name.split()
                if len(name_parts) >= 2:
                    name = name or name_parts[0]
                    surname = surname or name_parts[-1]
                else:
                    # Last resort fallback
                    name = name or display_name or 'User'
                    surname = surname or 'Unknown'
            
            tecnico_id = generate_tecnico_id(name, surname)
            
            # Get user photo
            photo = get_user_photo_from_graph(result['access_token'])
            
            # Create new tecnico
            tecnico = Tecnico(
                id=tecnico_id,
                name=name,
                last_name=surname,
                entity_email=graph_data.get('mail'),
                azure_id=session["user"]["oid"],
                azure_email=graph_data.get('mail'),
                azure_display_name=graph_data.get('displayName'),
                picture=photo,
                active=True,
                last_update=datetime.utcnow()
            )
            db.session.add(tecnico)
            db.session.commit()
        
        # Update last login
        tecnico.last_login = datetime.utcnow()
        db.session.commit()
        
        return redirect(url_for("index"))
    except ValueError:
        return redirect(url_for("login"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("show_login"))

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

# ===============================
# Models (Existing and New)
# ===============================

# --- Original Models ---

class Tecnico(db.Model):
    __tablename__ = 'tecnicos'  # Changed from 'employers'
    id = db.Column(db.String(50), primary_key=True)  # Will be generated as first_letter + surname + random
    name = db.Column(db.String)
    last_name = db.Column(db.String)
    second_last_name = db.Column(db.String)
    phone_number = db.Column(db.String(20))
    mobile_number = db.Column(db.String(20))
    personal_email = db.Column(db.String)
    entity_email = db.Column(db.String)
    address_id = db.Column(db.String(50), db.ForeignKey('address.address_id'))
    username = db.Column(db.String)
    password = db.Column(db.String)
    picture = db.Column(db.LargeBinary)
    active = db.Column(db.Boolean, default=True)
    last_update = db.Column(db.DateTime, default=datetime.utcnow)
    department_id = db.Column(db.String(50), db.ForeignKey('departments.department_id'))
    azure_id = db.Column(db.String(100), unique=True)
    azure_email = db.Column(db.String(255))
    azure_display_name = db.Column(db.String(255))
    last_login = db.Column(db.DateTime)

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
            text("SELECT id FROM tecnicos WHERE name = :nombre"),
            {"nombre": target.entidad_asignada}
        ).first()
        
        if not result:
            print(f"Error: No tecnico found with name={target.entidad_asignada}")
            return
            
    except Exception as e:
        print(f"Error updating tecnico assignments: {str(e)}")
        traceback.print_exc()

# --- New Models (New Schema) ---

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
    technician_id = db.Column(db.String(50), db.ForeignKey('tecnicos.id'), nullable=True)

    # Relationship with User model
    user = db.relationship('User', backref=db.backref('itinerario', lazy=True))
    # Add relationship with Tecnico model
    technician = db.relationship('Tecnico', backref=db.backref('itinerarios', lazy=True))

# ===============================
# Routes (Template Views)
# ===============================

@app.route('/projects')
@login_required
def projects():
    projects = Project.query.all()
    return render_template('projects.html', projects=projects)

@app.route('/project/<project_id>')
@login_required
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
@login_required
def add_users_to_project():
    try:
        project_id = request.form.get('project_id')
        selected_users = json.loads(request.form.get('users', '[]'))
        technician_id = request.form.get('technician_id')
        
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
                technician_id=technician_id,
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
@login_required
def get_user_profile(user_id):
    user = Itinerario.query.get_or_404(user_id)
    return jsonify({
        'success': True,
        'html': render_template('_user_profile.html', user=user)
    })

@app.route('/add_user_itinerario/<user_id>', methods=['POST'])
@login_required
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
@login_required
def edit_user(user_id):
    user = User.query.filter_by(dni_nie=user_id).first_or_404()
    return render_template('edit_user.html', user=user)

@app.route('/user_profile/<user_id>')
@login_required
def user_profile(user_id):
    user = User.query.filter_by(dni_nie=user_id).first_or_404()
    itinerario = db.session.get(Itinerario, user_id)
    return render_template('user_profile.html', 
                         user=user, 
                         itinerario=itinerario,
                         Project=Project)  # Pass the Project model to the template

@app.route('/update_user_itinerario/<user_id>', methods=['POST'])
@login_required
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
@login_required
def new_project():
    return render_template('new_project.html')

@app.route('/add_project', methods=['POST'])
@login_required
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
@login_required
def usuarios():
    return render_template('usuarios.html')

@app.route('/tecnicos')
@login_required
def tecnicos():
    return render_template('tecnicos.html')

@app.route('/employers')
@login_required
def employers():
    return render_template('employers.html')

@app.route('/addresses')
@login_required
def addresses():
    return render_template('addresses.html')

@app.route('/cities')
@login_required
def cities():
    return render_template('cities.html')

@app.route('/provinces')
@login_required
def provinces():
    return render_template('provinces.html')

@app.route('/entities')
@login_required
def entities():
    return render_template('entities.html')

@app.route('/departments')
@login_required
def departments():
    return render_template('departments.html')

@app.route('/new_users')
@login_required
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
@login_required
def id_docs():
    return render_template('id_docs.html')

@app.route('/social_groups')
@login_required
def social_groups():
    groups = SocialGroup.query.all()
    return jsonify([{
        'social_group_id': g.social_group_id,
        'group_name': g.group_name
    } for g in groups])

def generate_address_id():
    """Generate a unique address ID"""
    return f"ADDR_{random.randint(1000000000, 9999999999)}"

@app.route("/tecnico_profile")
@login_required
def tecnico_profile():
    # Get the current tecnico based on the Azure ID from the session
    tecnico = Tecnico.query.filter_by(azure_id=session["user"]["oid"]).first_or_404()
    return render_template('tecnico_profile.html', tecnico=tecnico)

# New route to get assigned users for a technician
@app.route('/get_assigned_users/<tecnico_id>')
@login_required
def get_assigned_users(tecnico_id):
    try:
        # Get the technician to get their name for legacy assignments
        tecnico = db.session.get(Tecnico, tecnico_id)
        if not tecnico:
            return jsonify({"error": "Technician not found"}), 404

        # Query legacy users that have this technician assigned by entity name
        legacy_users = User.query.filter_by(entidad_asignada=tecnico.name).all()
        
        # Also get users from itinerario table with this technician by ID
        itinerario_users = db.session.query(Itinerario)\
            .filter_by(technician_id=tecnico_id)\
            .all()
        
        # Combine both sets of users
        user_list = []
        
        # Add legacy users
        for user in legacy_users:
            user_list.append({
                'dni_nie': user.dni_nie,
                'nombre': user.nombre,
                'apellido1': user.apellido1,
                'apellido2': user.apellido2,
                'projects': user.projects or []
            })
        
        # Add users from itinerario if not already in list
        for itinerario in itinerario_users:
            if not any(u['dni_nie'] == itinerario.dni for u in user_list):
                user = db.session.get(User, itinerario.dni)
                if user:
                    user_list.append({
                        'dni_nie': user.dni_nie,
                        'nombre': user.nombre,
                        'apellido1': user.apellido1,
                        'apellido2': user.apellido2,
                        'projects': user.projects or []
                    })
        
        print(f"Found {len(user_list)} assigned users for technician {tecnico_id}")  # Debug log
        return jsonify(user_list)
    except Exception as e:
        print(f"Error getting assigned users: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/get_users')
@login_required
def get_users():
    try:
        users = User.query.all()
        user_list = [{
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
        } for user in users]
        return jsonify(user_list)
    except Exception as e:
        print(f"Error in get_users: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/get_cities_list')
@login_required
def get_cities_list():
    try:
        cities = City.query.all()
        return jsonify([{
            'id': c.city_id,
            'name': c.city,
            'province_id': c.province_id
        } for c in cities])
    except Exception as e:
        print(f"Error getting cities: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_entities_list')
@login_required
def get_entities_list():
    try:
        entities = Entity.query.all()
        return jsonify([{
            'id': e.entity_id,
            'name': e.name
        } for e in entities])
    except Exception as e:
        print(f"Error getting entities: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_social_groups_list')
@login_required
def get_social_groups_list():
    try:
        groups = SocialGroup.query.all()
        return jsonify([{
            'id': g.social_group_id,
            'name': g.group_name
        } for g in groups])
    except Exception as e:
        print(f"Error getting social groups: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_id_docs')
@login_required
def get_id_docs():
    try:
        docs = IdDoc.query.all()
        return jsonify([{
            'doc_type_id': d.doc_type_id,
            'doc_name': d.doc_name,
            'doc_template': d.doc_template,
            'doc_type_di': d.doc_type_di,
            'cert_extr_id': d.cert_extr_id,
            'vida_laboral_id': d.vida_laboral_id
        } for d in docs])
    except Exception as e:
        print(f"Error getting ID docs: {str(e)}")
        return jsonify({"error": str(e)}), 500

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
@login_required
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
@login_required
def edit_itinerario(user_id):
    user = User.query.filter_by(dni_nie=user_id).first_or_404()
    itinerario = Itinerario.query.get_or_404(user_id)
    return render_template('edit_itinerario.html', user=user, itinerario=itinerario)

@app.route('/add_new_user', methods=['POST'])
@login_required  # Add login_required decorator
def add_new_user():
    try:
        print("Received form data:", request.form)
        print("Received files:", request.files)
        
        if not session.get("user"):
            return jsonify({"error": "Session expired. Please login again."}), 401
        
        # Map social groups to colectivo values
        social_group_to_colectivo = {
            'Inactivo': 'Desemplead@',
            'Discapacidad': 'Discapacidad',
            'Mayores': 'Mayores',
            'Exclusión': 'Exclusión',
            'Inmigrante': 'Inmigrantes',
            'Joven sin experiencia laboral': 'Jóvenes sin experiencia laboral',
            'Mayores de 45': 'Mayores de 45'
        }

        # Get the social group name
        social_group = None
        if request.form.get('social_group_id'):
            social_group = db.session.get(SocialGroup, request.form.get('social_group_id'))
            
        # Map to correct colectivo value or use default
        colectivo = social_group_to_colectivo.get(
            social_group.group_name if social_group else 'Inactivo',
            'Desemplead@'  # Default value if mapping not found
        )

        # Validate required fields
        required_fields = ['doc_number', 'name', 'last_name']
        for field in required_fields:
            if not request.form.get(field):
                return jsonify({"error": f"Campo requerido faltante: {field}"}), 400

        # Create legacy User record
        legacy_user = User(
            dni_nie=request.form.get('doc_number'),
            nombre=request.form.get('name'),
            apellido1=request.form.get('last_name'),
            apellido2=request.form.get('second_last_name'),
            telefono=request.form.get('phone_number') or request.form.get('mobile_number'),
            colectivo=colectivo,  # Use mapped value
            acciones=request.form.get('actions', 'Espera'),  # Default to 'Espera' if not provided
            incidencia=request.form.get('incident'),
            entidad_asignada=db.session.get(Entity, request.form.get('entity_id')).name if request.form.get('entity_id') else "Prodiversa",
            acceso_programa="Sí",
            sex=request.form.get('sex'),
            birth_date=datetime.strptime(request.form.get('birth_date'), '%Y-%m-%d').date() if request.form.get('birth_date') else None,
            projects=[],
            files=[]
        )

        print(f"Created legacy user with colectivo: {legacy_user.colectivo}")  # Debug log

        db.session.add(legacy_user)
        db.session.commit()

        socketio.emit('update', {'message': 'new user added', 'table': 'users'})
        return jsonify({"success": True, "message": "Usuario creado exitosamente"})

    except Exception as e:
        db.session.rollback()
        print(f"Error in add_new_user: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/get_tecnicos')
@login_required
def get_tecnicos():
    try:
        tecnicos = Tecnico.query.filter_by(active=True).all()
        return jsonify([{
            'id': tecnico.id,
            'nombre': tecnico.name,
            'last_name': tecnico.last_name,
            'second_last_name': tecnico.second_last_name
        } for tecnico in tecnicos])
    except Exception as e:
        print(f"Error getting tecnicos: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

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
            if not Tecnico.query.filter_by(name=name).first():
                tecnico = Tecnico(
                    id=f"TEC_{name[:2].upper()}_{random.randint(1000, 9999)}",
                    name=name,
                    last_name=name,
                    second_last_name=name,
                    phone_number=f"555-{random.randint(100, 999)}-{random.randint(1000, 9999)}",
                    mobile_number=f"555-{random.randint(100, 999)}-{random.randint(1000, 9999)}",
                    personal_email=f"{name.lower()}@example.com",
                    entity_email=f"{name.lower()}@example.com",
                    address_id=generate_address_id(),
                    username=name.lower(),
                    password=f"{name.lower()}_password123",
                    picture=None,
                    active=True,
                    last_update=datetime.utcnow()
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
        
        # Add default document types if they don't exist
        default_docs = [
            {
                'doc_type_id': 'DOC_DNI',
                'doc_name': 'DNI',
                'doc_template': 'D'
            },
            {
                'doc_type_id': 'DOC_NIE',
                'doc_name': 'NIE',
                'doc_template': 'N'
            },
            {
                'doc_type_id': 'DOC_PASAPORTE',
                'doc_name': 'Pasaporte',
                'doc_template': 'P'
            }
        ]
        
        for doc in default_docs:
            if not IdDoc.query.filter_by(doc_type_id=doc['doc_type_id']).first():
                new_doc = IdDoc(**doc)
                try:
                    db.session.add(new_doc)
                    db.session.commit()
                    print(f"Created document type: {doc['doc_name']}")
                except Exception as e:
                    print(f"Error adding document type {doc['doc_name']}: {str(e)}")
                    db.session.rollback()
        
        try:
            db.session.commit()
        except Exception as e:
            print(f"Error adding default data: {str(e)}")
            db.session.rollback()
    # Add SSL context for HTTPS
    ssl_context = (
        'ssl/certificate.crt',  # Path relative to your app.py
        'ssl/private.key'       # Path relative to your app.py
    )

    socketio.run(app, host='0.0.0.0', port=5050, ssl_context=ssl_context)

# Add this after the app initialization but before the routes
@app.context_processor
def inject_tecnico():
    if session.get("user"):
        tecnico = Tecnico.query.filter_by(azure_id=session["user"]["oid"]).first()
        return {'tecnico': tecnico}
    return {'tecnico': None}