from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, event, update
import uuid
import re

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app)

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
        db.CheckConstraint('colectivo IN ("Desemplead@", "Discapacidad", "Mayores", "Exclusión", "Inmigrantes", "Jóvenes sin experiencia laboral", "Mayores de 45")', name='check_colectivo'),
        db.CheckConstraint('acciones IN ("Espera", "Citada", "Atendida", "No interesa", "Ocupada", "No acude", "Derivada", "No contesta")', name='check_acciones'),
        db.CheckConstraint('incidencia IN ("Error de conexión", "No hay información", "Baja administrativa", "Participante con otra entidad", "Error NIE")', name='check_incidencia'),
        db.CheckConstraint('entidad_asignada IN ("Prodiversa", "Mitad del cielo", "Acompanya", "Forprocer")', name='check_entidad'),
        db.CheckConstraint('acceso_programa IN ("Sí", "No")', name='check_acceso')
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

@app.route('/usuarios')
def usuarios():
    return render_template('usuarios.html')

@app.route('/tecnicos')
def tecnicos():
    return render_template('tecnicos.html')

@app.route('/add_user', methods=['POST'])
def add_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415
        
    try:
        data = request.get_json()
        
        # Validate DNI/NIE format
        dni_validation, dni_error = validate_dni_nie(data.get('dni_nie', ''))
        if not dni_validation:
            return jsonify({"error": dni_error}), 400
        
        # Normalize DNI/NIE to uppercase
        dni_normalized = data['dni_nie'].upper()
        
        # Check if DNI/NIE already exists
        if User.query.filter_by(dni_nie=dni_normalized).first():
            return jsonify({"error": "DNI/NIE ya existe en la base de datos"}), 400

        # Validate required fields
        required_fields = ['nombre', 'apellido1', 'telefono', 
                          'colectivo', 'acciones', 'entidad_asignada', 'acceso_programa']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Campo requerido faltante: {field}"}), 400

        # Validate constrained values
        validations = {
            'colectivo': ["Desemplead@", "Discapacidad", "Mayores", "Exclusión", 
                         "Inmigrantes", "Jóvenes sin experiencia laboral", "Mayores de 45"],
            'acciones': ["Espera", "Citada", "Atendida", "No interesa", 
                        "Ocupada", "No acude", "Derivada", "No contesta"],
            'entidad_asignada': ["Prodiversa", "Mitad del cielo", "Acompanya", "Forprocer"],
            'acceso_programa': ["Sí", "No"]
        }

        for field, allowed_values in validations.items():
            if data[field] not in allowed_values:
                return jsonify({"error": f"Valor inválido para {field}"}), 400

        if data.get('incidencia') and data['incidencia'] not in ["Error de conexión", "No hay información", 
                                                               "Baja administrativa", "Participante con otra entidad", 
                                                               "Error NIE"]:
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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route('/get_users')
def get_users():
    users = User.query.all()
    return jsonify([{
        'id': user.id,
        'dni_nie': user.dni_nie,
        'gesprodi': user.gesprodi,
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

@app.route('/get_tecnicos')
def get_tecnicos():
    tecnicos = Tecnico.query.all()
    return jsonify([{
        'id': t.id,
        'nombre': t.nombre,
        'area': t.area,
        'inserciones': t.inserciones
    } for t in tecnicos])

@socketio.on('connect')
def handle_connect():
    emit('update', {'message': 'connected'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, host='0.0.0.0', port=5050, debug=True)