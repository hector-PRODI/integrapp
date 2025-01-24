from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, event, update
import uuid

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
    id = db.Column(db.Integer, primary_key=True)
    dni = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(50), nullable=False)
    apellidos = db.Column(db.String(50), nullable=False)
    estado = db.Column(db.String(20), nullable=False, 
                      server_default='Desempleado',
                      info={'check_constraint': 'estado IN ("Desempleado", "Discapacitado", "Prestacion")'})
    tecnico = db.Column(db.String(50), db.ForeignKey('tecnico.nombre'), nullable=False)
    tecnico_rel = db.relationship('Tecnico', backref='users')

@event.listens_for(User, 'after_insert')
def after_user_insert(mapper, connection, target):
    try:
        stmt = update(Tecnico)\
            .where(Tecnico.nombre == target.tecnico)\
            .values(inserciones=Tecnico.inserciones + 1)
        connection.execute(stmt)
    except Exception as e:
        print(f"Error updating inserciones: {str(e)}")

@event.listens_for(User, 'after_delete')
def after_user_delete(mapper, connection, target):
    try:
        stmt = update(Tecnico)\
            .where(Tecnico.nombre == target.tecnico)\
            .values(inserciones=Tecnico.inserciones - 1)
        connection.execute(stmt)
    except Exception as e:
        print(f"Error updating inserciones: {str(e)}")

@event.listens_for(User, 'after_update')
def after_user_update(mapper, connection, target):
    try:
        hist = db.inspect(target).attrs.tecnico.history
        old_tecnico = hist.deleted[0] if hist.deleted else None
        new_tecnico = target.tecnico
        
        if old_tecnico:
            stmt = update(Tecnico)\
                .where(Tecnico.nombre == old_tecnico)\
                .values(inserciones=Tecnico.inserciones - 1)
            connection.execute(stmt)
        
        stmt = update(Tecnico)\
            .where(Tecnico.nombre == new_tecnico)\
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
        valid_estados = ['Desempleado', 'Discapacitado', 'Prestacion']
        
        if data['estado'] not in valid_estados:
            return jsonify({"error": "Invalid Estado value"}), 400
            
        if not Tecnico.query.filter_by(nombre=data['tecnico']).first():
            return jsonify({"error": "Técnico does not exist"}), 400
            
        new_user = User(
            dni=data['dni'],
            nombre=data['nombre'],
            apellidos=data['apellidos'],
            estado=data['estado'],
            tecnico=data['tecnico']
        )
        db.session.add(new_user)
        db.session.commit()
        socketio.emit('update', {'message': 'new user added'})
        return jsonify(success=True)
        
    except KeyError as e:
        db.session.rollback()
        return jsonify({"error": f"Missing field: {str(e)}"}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "DNI must be unique"}), 400
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
        return jsonify({"error": "Técnico name must be unique"}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/get_users')
def get_users():
    users = User.query.all()
    return jsonify([{
        'dni': user.dni,
        'nombre': user.nombre,
        'apellidos': user.apellidos,
        'estado': user.estado,
        'tecnico': user.tecnico
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

@app.route('/estado_stats')
def estado_stats():
    stats = db.session.query(
        User.estado,
        func.count(User.estado)
    ).group_by(User.estado).all()
    
    return jsonify({
        'labels': [result[0] for result in stats],
        'data': [result[1] for result in stats]
    })

@socketio.on('connect')
def handle_connect():
    emit('update', {'message': 'connected'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, host='0.0.0.0', port=5050, debug=True)