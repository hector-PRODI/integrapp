from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy.exc import IntegrityError
from sqlalchemy import update, event
import uuid
import re
import os
import hashlib
import traceback
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'bin'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def ensure_upload_folder():
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])

def generate_file_hash(file_data):
    return hashlib.sha256(file_data).hexdigest()

def save_file_with_hash(file):
    print(f"Saving file: {file.filename}")  # Debug log
    if file and allowed_file(file.filename):
        try:
            # Read file data and generate hash
            try:
                file_data = file.read()
            except ValueError:
                # If file has already been read, seek to beginning
                file.seek(0)
                file_data = file.read()
            
            file_hash = generate_file_hash(file_data)
            print(f"Generated hash: {file_hash}")  # Debug log
            
            # Create filename with hash
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[1].lower()
            new_filename = f"{file_hash}.{ext}"
            print(f"New filename: {new_filename}")  # Debug log
            
            # Save file
            ensure_upload_folder()
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
            print(f"File path: {file_path}")  # Debug log
            
            # Seek to beginning of file before writing
            file.seek(0)
            file.save(file_path)
            print("File saved successfully")  # Debug log
            
            return file_hash
        except Exception as e:
            print(f"Error in save_file_with_hash: {str(e)}")  # Debug log
            traceback.print_exc()
            raise
    return None

db = SQLAlchemy(app)
socketio = SocketIO(app)

# ===============================
# Models (Existing and New)
# ===============================

# --- Original Models ---

class Tecnico(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nombre = db.Column(db.String(50), unique=True, nullable=False)
    area = db.Column(db.String(50), nullable=False)
    inserciones = db.Column(db.Integer, default=0)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
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

    __table_args__ = (
        db.CheckConstraint(
            'colectivo IN ("Desemplead@", "Discapacidad", "Mayores", "Exclusión", "Inmigrantes", "Jóvenes sin experiencia laboral", "Mayores de 45")',
            name='check_colectivo'
        ),
        db.CheckConstraint(
            'acciones IN ("Espera", "Citada", "Atendida", "No interesa", "Ocupada", "No acude", "Derivada", "No contesta")',
            name='check_acciones'
        ),
        db.CheckConstraint(
            'incidencia IN ("Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE")',
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
        stmt = update(Tecnico)\
            .where(Tecnico.nombre == target.entidad_asignada)\
            .values(inserciones=Tecnico.inserciones + 1)
        connection.execute(stmt)
    except Exception as e:
        print(f"Error updating inserciones: {str(e)}")

# --- New Models (New Schema) ---

class Employer(db.Model):
    __tablename__ = 'employers'
    employer_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String)
    last_name = db.Column(db.String)
    second_last_name = db.Column(db.String)
    phone_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    mobile_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    personal_email = db.Column(db.String)
    entity_email = db.Column(db.String)
    address_id = db.Column(db.BigInteger, db.ForeignKey('address.address_id'))
    username = db.Column(db.String)
    password = db.Column(db.String)
    picture = db.Column(db.LargeBinary)
    active = db.Column(db.Boolean, default=True)
    last_update = db.Column(db.DateTime, default=datetime.utcnow)
    department_id = db.Column(db.SmallInteger, db.ForeignKey('departments.department_id'))

class Address(db.Model):
    __tablename__ = 'address'
    address_id = db.Column(db.BigInteger, primary_key=True)
    address = db.Column(db.String)
    address2 = db.Column(db.String)
    postal_code = db.Column(db.SmallInteger)
    city_id = db.Column(db.BigInteger, db.ForeignKey('city.city_id'))

class City(db.Model):
    __tablename__ = 'city'
    city_id = db.Column(db.BigInteger, primary_key=True)
    city = db.Column(db.String)
    province_id = db.Column(db.SmallInteger, db.ForeignKey('provinces.province_id'))

class Province(db.Model):
    __tablename__ = 'provinces'
    province_id = db.Column(db.SmallInteger, primary_key=True)
    province = db.Column(db.String)

class Entity(db.Model):
    __tablename__ = 'entity'
    entity_id = db.Column(db.SmallInteger, primary_key=True)
    name = db.Column(db.String)

class Department(db.Model):
    __tablename__ = 'departments'
    department_id = db.Column(db.SmallInteger, primary_key=True)
    name = db.Column(db.String)
    entity_id = db.Column(db.SmallInteger, db.ForeignKey('entity.entity_id'))

# Note: To avoid name conflicts, we name this model "UserNew"
class UserNew(db.Model):
    __tablename__ = 'users'
    user_no = db.Column(db.SmallInteger, primary_key=True)
    doc_type_id = db.Column(db.SmallInteger, db.ForeignKey('id_docs.doc_type_id'))
    doc_number = db.Column(db.String)
    name = db.Column(db.String)
    last_name = db.Column(db.String)
    second_last_name = db.Column(db.String)
    phone_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    mobile_number = db.Column(db.String(20))  # Changed to String to handle longer phone numbers
    email = db.Column(db.String)
    technician_id = db.Column(db.Integer)  # Not linked as foreign key because of type mismatch
    social_group_id = db.Column(db.Integer, db.ForeignKey('social_groups.social_group_id'))
    address_id = db.Column(db.BigInteger, db.ForeignKey('address.address_id'))
    entity_id = db.Column(db.SmallInteger, db.ForeignKey('entity.entity_id'))
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)
    users_info_id = db.Column(db.BigInteger, db.ForeignKey('users_info.users_info_id'))
    actions = db.Column(db.String(20))  # New column for actions
    incident = db.Column(db.String(50))  # New column for incidents

    __table_args__ = (
        db.CheckConstraint(
            'actions IN ("Espera", "Citada", "Atendida", "No interesa", "Ocupada", "No acude", "Derivada", "No contesta")',
            name='check_actions'
        ),
        db.CheckConstraint(
            'incident IN ("Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE")',
            name='check_incident'
        )
    )

class IdDoc(db.Model):
    __tablename__ = 'id_docs'
    doc_type_id = db.Column(db.SmallInteger, primary_key=True)
    doc_name = db.Column(db.BigInteger)
    doc_template = db.Column(db.CHAR)
    doc_type_di = db.Column(db.String(64))  # Hash for DNI/NIF file
    cert_extr_id = db.Column(db.String(64))  # Hash for Certificado extranjería file
    vida_laboral_id = db.Column(db.String(64))  # Hash for Vida laboral file

class SocialGroup(db.Model):
    __tablename__ = 'social_groups'
    social_group_id = db.Column(db.Integer, primary_key=True)
    group_name = db.Column(db.String)

class UsersInfo(db.Model):
    __tablename__ = 'users_info'
    users_info_id = db.Column(db.BigInteger, primary_key=True)
    technician_observ = db.Column(db.String)

# ===============================
# Routes (Template Views)
# ===============================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/usuarios')
def usuarios():
    return render_template('usuarios.html')

@app.route('/tecnicos')
def tecnicos():
    return render_template('tecnicos.html')

@app.route('/employers')
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
                doc_type_id=1,
                doc_name=1,
                doc_template='T'
            )
            db.session.add(test_doc)
            db.session.commit()
            print("Created test IdDoc record")

        # Create test SocialGroup if it doesn't exist
        if not SocialGroup.query.first():
            test_group = SocialGroup(
                social_group_id=1,
                group_name='Test Group'
            )
            db.session.add(test_group)
            db.session.commit()
            print("Created test SocialGroup record")

        # Create test Entity if it doesn't exist
        if not Entity.query.first():
            test_entity = Entity(
                entity_id=1,
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
            incidencia=data.get('incidencia'),
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
        'observaciones': user.observaciones
    } for user in users])

# --- Tecnicos ---
@app.route('/add_tecnico', methods=['POST'])
def add_tecnico():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
    try:
        data = request.get_json()
        new_tecnico = Tecnico(
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
            department_id=data.get('department_id')
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
                postal_code=int(postal_code) if postal_code else None,
                city_id=int(city_id) if city_id else None
            )
            db.session.add(new_address)
            db.session.commit()
        else:
            address_id = None

        # Convert form data to appropriate types
        doc_number = request.form.get('doc_number')
        phone_number = request.form.get('phone_number')
        mobile_number = request.form.get('mobile_number')
        technician_id = int(request.form.get('technician_id')) if request.form.get('technician_id') else None
        social_group_id = int(request.form.get('social_group_id')) if request.form.get('social_group_id') else None
        entity_id = int(request.form.get('entity_id')) if request.form.get('entity_id') else None
        
        # Create UserNew record using doc_number as user_no
        new_user = UserNew(
            user_no=doc_number,  # Using doc_number as user_no
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
            create_date=datetime.strptime(request.form.get('create_date'), '%Y-%m-%dT%H:%M') if request.form.get('create_date') else datetime.utcnow(),
            active=request.form.get('active') == 'true',
            users_info_id=None,  # Removed this field
            actions=request.form.get('actions'),
            incident=request.form.get('incident')
        )
        db.session.add(new_user)
        db.session.commit()

        # Process and save files
        file_hashes = {}
        
        print("Processing files...")  # Debug log
        if 'dni_file' in request.files:
            print("Processing DNI file...")  # Debug log
            try:
                file_hash = save_file_with_hash(request.files['dni_file'])
                print(f"DNI file hash: {file_hash}")  # Debug log
                if file_hash:
                    file_hashes['doc_type_di'] = file_hash
            except Exception as e:
                print(f"Error saving DNI file: {str(e)}")  # Debug log
                traceback.print_exc()
        
        if 'cert_extr_file' in request.files:
            print("Processing cert_extr file...")  # Debug log
            try:
                file_hash = save_file_with_hash(request.files['cert_extr_file'])
                print(f"cert_extr file hash: {file_hash}")  # Debug log
                if file_hash:
                    file_hashes['cert_extr_id'] = file_hash
            except Exception as e:
                print(f"Error saving cert_extr file: {str(e)}")  # Debug log
                traceback.print_exc()
        
        if 'vida_laboral_file' in request.files:
            print("Processing vida_laboral file...")  # Debug log
            try:
                file_hash = save_file_with_hash(request.files['vida_laboral_file'])
                print(f"vida_laboral file hash: {file_hash}")  # Debug log
                if file_hash:
                    file_hashes['vida_laboral_id'] = file_hash
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

        # Also create a record in the User table
        user = User(
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
            acceso_programa="Sí"
        )
        db.session.add(user)
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
    socketio.run(app, host='0.0.0.0', port=5050, debug=True)
