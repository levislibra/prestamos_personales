# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

#from openerp.osv import osv, orm
#from datetime import time, datetime
#from openerp.tools.translate import _
#from openerp import models, fields

import pytz
import re
import time
import openerp
import openerp.service.report
import uuid
import collections
import babel.dates
from werkzeug.exceptions import BadRequest
from datetime import datetime, timedelta
from dateutil import parser
from dateutil import rrule
from dateutil.relativedelta import relativedelta
from openerp import api
from openerp import tools, SUPERUSER_ID
from openerp.osv import fields, osv
from openerp.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT
from openerp.tools.translate import _
from openerp.http import request
from operator import itemgetter
from openerp.exceptions import UserError
from openerp.exceptions import ValidationError
from openerp import models

import logging
from openerp.osv import orm

_logger = logging.getLogger(__name__)
#       _logger.error("date now : %r", date_now)

class prestamo_tipo(osv.Model):
    _name = 'prestamo.tipo'
    _description = 'Tipos de prestamos'
    _columns = {
        'name': fields.char("Nombre", size=64, required=True),
        'active': fields.boolean("Activo"),
    }
    _defaults = {
        'active': True,
    }


class prestamo_plan(osv.Model):
	_name = 'prestamo.plan'
	_description = 'Parametros para el calculo de cuotas'
	_columns = {
		'name': fields.char("Nombre", size=256, required=True),
		'codigo': fields.char("Codigo", size=8, required=True),
        'tipo': fields.many2one('prestamo.tipo', 'Tipo', required=True),
        'active': fields.boolean("Activo"),
        'recibo_de_sueldo': fields.boolean("Requiere recibo de sueldo?", required=True),
        'cuotas': fields.integer("Cuotas", required=True),
        'tasa_de_interes': fields.float("Tasa de interes mensual", required=True),
        'tasa_de_punitorios': fields.float("Tasa de punitorios mensual", required=True),
        'dias_de_gracia_punitorios': fields.integer("Dias de gracia para punitorios", required=True),
        'dias_entre_vencimientos': fields.integer("Dias entre vencimientos", required=True),
        'proporcional_primer_cuota': fields.boolean("Interes proporcional en primer cuota", required=True),
        'tipo_de_amortizacion': fields.selection([('sistema_directa', 'Sistema de tasa directa'), ('sistema_frances', 'Sistema frances'), ('sistema_aleman', 'Sistema aleman'), ('sistema_americano', 'Sistema americano')], string='Sistema de tasa', required=True, select=True,),
        'dia_diferimiento_cuota': fields.integer("Dia para el diferimiento mensual de la primer cuota"),
	}


#    _defaults = {
#        'codigo': "000",#lambda *a: time.strftime('%Y-%m-%d'),
#        'active': True,
#        'dias_entre_vencimientos': 30,
#    }

class prestamo_cuota(osv.Model):
    _name = 'prestamo.cuota'
    _description = 'Detalles cuota'
    _columns = {
        'name': fields.integer("Numero", required=True, readonly=True),
        'fecha_vencimiento': fields.date("Fecha vencimiento", required=True),
        'monto': fields.float("Monto", required=True),
        'saldo_capital': fields.float("Saldo capital", required=True),
        'punitorios': fields.float("Punitorios"),
        'ultima_fecha_cobro': fields.date("Ultima fecha de cobro"),
        'cobrado': fields.float("Cobrado"),
        'prestamo_prestamo_id': fields.many2one("prestamo.prestamo", "Prestamo", required=True),
        'saldo': fields.float("Saldo"),
    }


class prestamo_prestamo(osv.Model):
    _name = 'prestamo.prestamo'
    _description = 'Informacion del prestamo otorgado'
    _columns = {
        'name': fields.char("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'fecha_primer_vencimiento': fields.date("Fecha primer vencimiento", required=True),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
        'prestamo_plan_id': fields.many2one("prestamo.plan", "Plan de pagos", required=True),
        'prestamo_cuota_ids': fields.one2many("prestamo.cuota", "prestamo_prestamo_id", "Cuotas"),
    }

    _defaults = {
        'fecha': lambda *a: time.strftime('%Y-%m-%d'),
        'fecha_primer_vencimiento': lambda *a: time.strftime('%Y-%m-%d'),
    }

class prestamo_obseracion(osv.Model):
    _name = 'prestamo.observacion'
    _description = 'Informacion de observaciones'
    _columns = {
        'name': fields.char("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'detalle': fields.char("Lo conversado", required=True),
        'fecha_proxima_accion': fields.date("Fecha para la proxima accion"),
        'accion': fields.char("Accion a realizar"),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
    }

class prestamo_recibo(osv.Model):
    _name = 'prestamo.recibo'
    _description = 'Informacion de recibos'
    _columns = {
        'name': fields.char("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'monto': fields.float("Monto", required=True),
        'prestamo_cuota_id': fields.many2one("prestamo.cuota", "Cuota", required=True),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
    }

class prestamo_cuenta(osv.Model):
    _name = 'prestamo.cuenta'
    _description = 'Detalles de la cuenta de una cliente'
    _columns = {
        'name': fields.char("ID", readonly=True),
        'cliente_id': fields.many2one("res.partner", "Cliente", required=True),
        'recibo_de_sueldo': fields.boolean("Tiene recibo de sueldo?"),
        'limite_credito': fields.float("Maximo monto a otorgar"),
        'ingresos_comprobables': fields.float("Ingresos comprobables"),
        'fecha_inicio_trabajo_actual': fields.date("Fecha inicio trabajo actual"),
        'especificacion_laboral': fields.selection([('empleado', 'Empleado'), ('autonomo', 'Autonomo'), ('desempleado', 'Desempleado'), ('jubilado', 'Jubilado'), ('pensionado', 'Pensionado')], string='Especificacion Laboral'),
        'prestamo_prestamo_ids': fields.one2many("prestamo.prestamo", "prestamo_cuenta_id", "Prestamos"),
        'prestamo_observacion_ids': fields.one2many("prestamo.observacion", "prestamo_cuenta_id", "Observaciones"),
        'prestamo_recibo_ids': fields.one2many("prestamo.recibo", "prestamo_cuenta_id", "Recibos"),
        'active': fields.boolean("Activo"),
        'state': fields.selection([('borrador', 'Borrador'), ('confirmado', 'Confirmado')], string='estado', readonly=True),
        'Saldo': fields.float("Saldo", readonly=True),
    }
