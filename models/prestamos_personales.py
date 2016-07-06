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
import pprint
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
		'name': fields.char("Nombre", size=64, required=True),
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
        'id': fields.integer("ID", required=True, readonly=True),
        'numero_cuota': fields.integer("Numero de cuota", required=True, readonly=True),
        'fecha_vencimiento': fields.date("Fecha vencimiento", required=True),
        'capital_saldo': fields.float("Saldo capital", required=True),

        #Componentes del monto de la cuota
        'capital_cuota': fields.float("Capital", required=True),
        'interes_cuota': fields.float("Interes", required=True),
        'iva_cuota': fields.float("IVA", required=True),
        'punitorios_cuota': fields.float("Punitorios"),
        'cobrado_cuota': fields.float("Cobrado"),
        'monto_cuota': fields.float("Total cuota", readonly=True),

        'ultima_fecha_cobro_cuota': fields.date("Ultima fecha de cobro"),
        'prestamo_prestamo_id': fields.many2one("prestamo.prestamo", "Prestamo"),
        'prestamo_recibo_id': fields.many2one("prestamo.recibo", "Recibo"),

        #Para visualizar cuotas pendientes en prestamo.cuenta
        'cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta"),
        'state': fields.selection([('borrador', 'Borrador'), ('activa', 'Activa'), ('cobrada', 'Cobrada'), ('moraTemprana', 'Mora temprana'), ('moraMedia', 'Mora media'), ('moraTardia', 'Mora tardia'), ('incobrable', 'Incobrable')], string='Estado', readonly=True),
    }
    _defaults = {
        'state': "borrador",
    }


class prestamo_prestamo(osv.Model):
    _name = 'prestamo.prestamo'
    _description = 'Informacion del prestamo otorgado'
    _rec_name = "display_name"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'display_name': fields.char("Prestamo", compute="_compute_display_name"),
        'fecha_primer_vencimiento': fields.date("Fecha primer vencimiento", required=True),
        'monto_otorgado': fields.float("Monto Otorgado", required=True),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
        'prestamo_plan_id': fields.many2one("prestamo.plan", "Plan de pagos", required=True),
        'prestamo_cuota_ids': fields.one2many("prestamo.cuota", "prestamo_prestamo_id", "Cuotas", ondelete='cascade'),
        'state': fields.selection([('borrador', 'Borrador'), ('confirmado', 'Confirmado'), ('pagado', 'Pagado')], string='Estado', readonly=True),
    }

    @api.one
    @api.depends('prestamo_plan_id')
    def _compute_display_name(self):
        if self.prestamo_plan_id != False:
            self.display_name = "Prestamo " + str(self.id) + " - " + self.prestamo_plan_id.name

    _defaults = {
        'fecha': lambda *a: time.strftime('%Y-%m-%d'),
        'fecha_primer_vencimiento': lambda *a: time.strftime('%Y-%m-%d'),
        'state': "borrador",
    }

    @api.multi
    def confirmar(self):
        self.state = "confirmado"
        for cuota in self.prestamo_cuota_ids:
            cuota.state = "activa"


    @api.multi
    def calcular_cuotas_plan(self):

        if len(self.prestamo_cuota_ids) > 0:
            for cuota in self.prestamo_cuota_ids:
                cuota.unlink()


        if self.prestamo_plan_id.cuotas != False:
            cantidad_de_cuotas = self.prestamo_plan_id.cuotas
            capital_saldo = self.monto_otorgado
            capital_cuota = self.monto_otorgado / cantidad_de_cuotas

            cuota_ids = []
            i = 1
            while i <= cantidad_de_cuotas:
                tasa_de_interes_dia = self.prestamo_plan_id.tasa_de_interes / 30
                dias = self.prestamo_plan_id.dias_entre_vencimientos * i
                interes_cuota = capital_cuota * tasa_de_interes_dia * dias
                iva_cuota =  interes_cuota * 0.21
                monto_cuota = capital_cuota + interes_cuota + iva_cuota

                val = {
                    'numero_cuota': i,
                    'fecha_vencimiento': self.fecha_primer_vencimiento,
                    'capital_saldo': capital_saldo,

                    #Componentes del monto de la cuota
                    'capital_cuota': capital_cuota,
                    'interes_cuota': interes_cuota,
                    'iva_cuota': iva_cuota,
                    'punitorios_cuota': 0.0,
                    'cobrado_cuota': 0.0,
                    'monto_cuota': monto_cuota,

                    'prestamo_prestamo_id': self.id,
                    'state': 'borrador',
                }

                #pt = {
                #    'name': "Prestamo 12 cuotas",
                #}
                #prestamo_tipo = self.env['prestamo.tipo'].create(pt)

                #prestamo_cuota_n = self.pool.get('prestamo.cuota').create(self.env['cr'], self.env['uid'], val)
                prestamo_cuota_n = self.env['prestamo.cuota'].create(val)
                cuota_ids.append(prestamo_cuota_n.id)
                i = i + 1
            self.prestamo_cuota_ids = cuota_ids


class prestamo_obseracion(osv.Model):
    _name = 'prestamo.observacion'
    _description = 'Informacion de observaciones'
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'detalle': fields.char("Lo conversado", required=True),
        'fecha_proxima_accion': fields.date("Fecha para la proxima accion"),
        'accion': fields.char("Accion a realizar"),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
    }

class prestamo_recibo(osv.Model):
    _name = 'prestamo.recibo'
    _description = 'Informacion de recibos'
    _rec_name="id"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'monto': fields.float("Monto", required=True),
        'journal_id': fields.many2one('account.journal', string="Metodo de Cobro", required=True, domain="[('type', 'in', ('bank', 'cash'))]"),
        'prestamo_cuota_ids': fields.one2many("prestamo.cuota", 'prestamo_recibo_id',"Cuota", required=True),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),

        #'prestamo_cuota_ids': fields.many2many("prestamo.cuota", "prestamo_recibo_ids", "Cuotas"),
    }

    _defaults = {
        'fecha': lambda *a: time.strftime('%Y-%m-%d'),
    }

    @api.model
    def default_get(self, fields):
        rec = super(prestamo_recibo, self).default_get(fields)
        context = dict(self._context or {})
        active_model = context.get('active_model')
        active_ids = context.get('active_ids')

        _logger.error("rec : %r", rec)
        _logger.error("context : %r", context)
        _logger.error("active_model : %r", active_model)
        _logger.error("active_ids : %r", active_ids)

        # Checks on context parameters
        if not active_model or not active_ids:
            raise UserError(_("Programmation error: wizard action executed without active_model or active_ids in context."))
        if active_model != 'prestamo.cuota':
            raise UserError(_("Programmation error: the expected model for this action is 'account.invoice'. The provided one is '%d'.") % active_model)

        # Checks on received cuotas records
        cuotas = self.env[active_model].browse(active_ids)
#        if any(invoice.state != 'open' for invoice in invoices):
#            raise UserError(_("You can only register payments for open invoices"))
#        if any(inv.commercial_partner_id != invoices[0].commercial_partner_id for inv in invoices):
#            raise UserError(_("In order to pay multiple invoices at once, they must belong to the same commercial partner."))
#        if any(MAP_INVOICE_TYPE_PARTNER_TYPE[inv.type] != MAP_INVOICE_TYPE_PARTNER_TYPE[invoices[0].type] for inv in invoices):
#            raise UserError(_("You cannot mix customer invoices and vendor bills in a single payment."))
#        if any(inv.currency_id != invoices[0].currency_id for inv in invoices):
#            raise UserError(_("In order to pay multiple invoices at once, they must use the same currency."))

        total_amount = sum(cuota.monto_cuota for cuota in cuotas)

        rec.update({
            'monto': abs(total_amount),
            'prestamo_cuota_ids': active_ids,
#            'currency_id': invoices[0].currency_id.id,
#            'payment_type': total_amount > 0 and 'inbound' or 'outbound',
#            'partner_id': invoices[0].commercial_partner_id.id,
#            'partner_type': MAP_INVOICE_TYPE_PARTNER_TYPE[invoices[0].type],
        })
        return rec

class prestamo_cuenta(osv.Model):
    _name = 'prestamo.cuenta'
    _description = 'Detalles de la cuenta de una cliente'
    _rec_name = "cliente_id"
    _columns = {
        'id': fields.integer("ID", readonly=True),
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
        
        'cuota_ids': fields.one2many('prestamo.cuota', 'cuenta_id', compute="calcular_cuotas_pendientes", readonly=True),
        'Saldo': fields.float("Saldo", readonly=True),
    }

    @api.multi
    @api.depends('prestamo_prestamo_ids')
    def calcular_cuotas_pendientes(self):
        cuota_ids = []
        for prestamo in self.prestamo_prestamo_ids:
            for cuota in prestamo.prestamo_cuota_ids:
                if cuota.state == 'activa':
                    cuota_ids.append(cuota.id)
        self.cuota_ids = cuota_ids

    @api.multi
    def actualizar(self):
        cuota_ids = []
        for prestamo in self.prestamo_prestamo_ids:
            print "prestamo state:: "+prestamo.state
            for cuota in prestamo.prestamo_cuota_ids:
                print "cuota state:: "+cuota.state
                if cuota.state == 'activa':
                    print "cuota agregada:: " + str(cuota.numero_cuota)
                    cuota_ids.append(cuota.id)
                else:
                    cuota.cuenta_id = False
        self.cuota_ids = cuota_ids

    def confirmar(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, {'state':'confirmado'}, context=None)
        return True


    @api.multi
    def nuevo_prestamo(self):
        id = self.id
        return {
            'name': ('prestamos_personales'),
            'res_model': 'module.Model',
            'type': 'ir.actions.act_window',
            'context': {},
            'view_mode': 'form',
            'view_type': 'form',
            'view_id': self.env.ref('prestamo_prestamo_form'),
            'target': 'current',
        }

#    def nuevo_prestamo(self, cr, uid, ids, context=None):
#       pass
#        return True

    _sql_constraints = [
        ('id_uniq', 'unique (id)', "El ID ya existe!"),
        ('cliente_id_uniq', 'unique (cliente_id)', "El cliente ya tiene una cuenta asociada!"),
    ]
    _defaults = {
        'fecha_inicio_trabajo_actual': lambda *a: time.strftime('%Y-%m-%d'),
        'state': "borrador",
        'active': True,
    }
