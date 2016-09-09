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
from openerp import SUPERUSER_ID
import pprint
from openerp.tools import amount_to_text_en, float_round
import amount_to_text_es_MX
import math
import logging

from openerp.osv import orm

_logger = logging.getLogger(__name__)
#_logger.error("date now : %r", date_now)

class prestamo_tipo(osv.Model):
    _name = 'prestamo.tipo'
    _description = 'Tipos de prestamos'
    _columns = {
        'name': fields.char("Nombre", size=64, required=True),
        'active': fields.boolean("Activo"),
        'prestamo_mutuo_id': fields.many2one("prestamo.documentacion", "Mutuo"),
        'prestamo_pagare_id': fields.many2one("prestamo.documentacion", "Pagare"),
        'prestamo_cobro_cuota_id': fields.many2one("prestamo.documentacion", "Comprobante de cobro"),
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
        'recibo_de_sueldo': fields.boolean("Requiere recibo de sueldo?"),
        'cuotas': fields.integer("Cuotas", required=True),
        'tasa_de_interes': fields.float("Tasa de interes mensual", required=True, digits=(16,3)),
        'tasa_de_punitorios': fields.float("Tasa de punitorios mensual", required=True, digits=(16,3)),
        'dias_de_gracia_punitorios': fields.integer("Dias de gracia para punitorios", required=True),
        'dias_entre_vencimientos_select': fields.selection([('mensual', 'Mensual'), ('dias', 'Cantidad de dias')], string='Dias entre vencimientos', required=True, select=True),
        'dias_entre_vencimientos': fields.integer("Dias entre vencimientos", required=True),
        'iva_id': fields.many2one("account.tax", "Impuesto por defecto", domain="[('active', '=', True), ('type_tax_use', '=', 'sale')]", required=True),
        'iva_incluido': fields.boolean("IVA incluido en tasa de interes?"),
        'proporcional_primer_cuota': fields.boolean("Interes proporcional en primer cuota"),
        'tipo_de_amortizacion': fields.selection([('sistema_directa', 'Sistema de tasa directa'), ('sistema_frances', 'Sistema frances'), ('sistema_aleman', 'Sistema aleman'), ('sistema_americano', 'Sistema americano')], string='Sistema de tasa', required=True, select=True),
        'journal_id': fields.many2one('account.journal', 'Diario ventas/ingresos', domain="[('type', '=', 'sale')]", required=True),
        'journal_otros_ingresos_id': fields.many2one('account.journal', 'Diario otros ingresos', domain="[('type', '=', 'sale')]", required=True),
        #'cuenta_iva_id': fields.many2one('account.account', 'Cuenta IVA credito', required=True),
        'invoice':fields.boolean('Emitir factura'),
        'comision_de_apertura': fields.float("Comision de apertura (%)", help="Aplicado sobre monto otorgado.", digits=(16,3)),
        'gastos_de_gestion': fields.float("Gaston de gestion", help="Es el monto de gastos de gestion, el cual disminuye el monto otorgado al cliente."),
    }
#    _defaults = {
#        'codigo': "000",#lambda *a: time.strftime('%Y-%m-%d'),
#        'active': True,
#        'dias_entre_vencimientos': 30,
#    }

class prestamo_cuota(osv.Model):
    _name = 'prestamo.cuota'
    _description = 'Detalles cuota'
    _rec_name = "display_name"
    _columns = {
        'id': fields.integer("ID", required=True, readonly=True),
        'numero_cuota': fields.integer("Numero de cuota", required=True, readonly=True),
        'display_name': fields.char("Detalle", readonly=True, compute="compute_name"),
        'fecha_vencimiento': fields.date("Fecha vencimiento", required=True),
        'capital_saldo': fields.float("Saldo capital", required=True),

        #Componentes del monto de la cuota
        'capital_cuota': fields.float("Capital", required=True),
        'interes_cuota': fields.float("Interes", required=True),
        'iva_cuota': fields.float("IVA", required=True),
        'punitorios_cuota': fields.float("Punitorios"),
        'monto_cuota': fields.float("Total cuota", compute="_compute_monto_cuota", readonly=True),
        'cobrado_cuota': fields.float("Cobrado"),
        'saldo_cuota': fields.float("Saldo cuota", compute="_compute_monto_cuota", readonly=True),

        'ultima_fecha_cobro_cuota': fields.date("Ultima fecha de cobro"),
        'prestamo_prestamo_id': fields.many2one("prestamo.prestamo", "Prestamo"),
        'prestamo_recibo_id': fields.many2one("prestamo.recibo", "Recibo"),

        'state': fields.selection([('borrador', 'Borrador'), ('activa', 'Activa'), ('cobrada', 'Cobrada'), ('moraTemprana', 'Mora temprana'), ('moraMedia', 'Mora media'), ('moraTardia', 'Mora tardia'), ('incobrable', 'Incobrable')], string='Estado', readonly=True),
    }
    _defaults = {
        'state': "borrador",
    }

    def get_conceptos_de_cobro(self, monto):
        ret = {'capital': 0, 'interes': 0, 'iva': 0, 'punitorios': 0}
        cobrado = {'capital': 0, 'interes': 0, 'iva': 0, 'punitorios': 0}

        if self.cobrado_cuota > 0:
            monto_previo = self.cobrado_cuota
            if monto_previo > 0:
                cobrado['capital'] = min(self.capital_cuota, monto_previo)
                monto_previo = float("{0:.2f}".format(monto_previo - cobrado['capital']))
            if monto_previo > 0:
                cobrado['interes'] = min(self.interes_cuota, monto_previo)
                monto_previo = float("{0:.2f}".format(monto_previo - cobrado['interes']))
            if monto_previo > 0:
                cobrado['iva'] = min(self.iva_cuota, monto_previo)
                monto_previo = float("{0:.2f}".format(monto_previo - cobrado['iva']))
            if monto_previo > 0:
                cobrado['punitorios'] = min(self.punitorios_cuota, monto_previo)
                monto_previo = float("{0:.2f}".format(monto_previo - cobrado['punitorios']))

        if monto > 0:
            ret['capital'] = min(self.capital_cuota-cobrado['capital'], monto)
            monto = monto - ret['capital']
        if monto > 0:
            ret['interes'] = min(self.interes_cuota-cobrado['interes'], monto)
            monto = monto - ret['interes']
        if monto > 0:
            ret['iva'] = min(self.iva_cuota-cobrado['iva'], monto)
            monto = monto - ret['iva']
        if monto > 0:
            ret['punitorios'] = min(self.punitorios_cuota-cobrado['punitorios'], monto)
            monto = monto - ret['punitorios']

        return ret    

    @api.one
    @api.depends('numero_cuota')
    def compute_name(self):
        self.display_name = "[Prestamo: " + self.prestamo_prestamo_id.display_name + ", cuota: " + str(self.numero_cuota) + "]"

    @api.one
    def actpunitorios(self, cr):
        fecha_inicial = datetime.strptime(str(self.fecha_vencimiento), "%Y-%m-%d")
        fecha_final = datetime.strptime(str(time.strftime("%Y-%m-%d")), "%Y-%m-%d")
        diferencia = fecha_final - fecha_inicial
        monto = self.capital_cuota + self.interes_cuota + self.iva_cuota
        if diferencia.days > self.prestamo_prestamo_id.prestamo_plan_id.dias_de_gracia_punitorios:
            self.punitorios_cuota = monto * diferencia.days * (self.prestamo_prestamo_id.prestamo_plan_id.tasa_de_punitorios / 30)

    @api.one
    @api.depends('capital_cuota', 'interes_cuota', 'iva_cuota', 'punitorios_cuota', 'cobrado_cuota')
    def _compute_monto_cuota(self):
        self.monto_cuota = float("{0:.2f}".format(abs(self.capital_cuota + self.interes_cuota + self.iva_cuota + self.punitorios_cuota)))
        self.saldo_cuota = float("{0:.2f}".format(abs(self.capital_cuota + self.interes_cuota + self.iva_cuota + self.punitorios_cuota - self.cobrado_cuota)))

class prestamo_prestamo(osv.Model):
    _name = 'prestamo.prestamo'
    _description = 'Informacion del prestamo otorgado'
    _rec_name = "display_name"
    _order = "id desc"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'display_name': fields.char("Prestamo", compute="_compute_display_name"),
        'fecha_primer_vencimiento': fields.date("Fecha primer vencimiento", required=True),
        'monto_otorgado': fields.float("Monto a Financiar", required=True),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
        'prestamo_plan_id': fields.many2one("prestamo.plan", "Plan de pagos", required=True),
        'prestamo_cuota_ids': fields.one2many("prestamo.cuota", "prestamo_prestamo_id", "Cuotas", ondelete='cascade'),
        'state': fields.selection([('borrador', 'Borrador'), ('confirmado', 'Confirmado'), ('pagado', 'Pagado'), ('cancelado', 'Cancelado')], string='Estado', readonly=True),
        'prestamo_pago_id': fields.many2one("prestamo.pago", "Comprobante de pago", readonly=True),
        'prestamo_mutuo_text': fields.html("Mutuo"),
        'prestamo_pagare_text': fields.html("Pagare"),
    }

    _defaults = {
        'fecha': lambda *a: time.strftime('%Y-%m-%d'),
        'fecha_primer_vencimiento': lambda *a: time.strftime('%Y-%m-%d'),
        'state': "borrador",
    }

    @api.one
    @api.depends('prestamo_plan_id')
    def _compute_display_name(self):
        if self.prestamo_plan_id != False:
            self.display_name = "Prestamo " + str(self.id) + " - " + self.prestamo_plan_id.name

    @api.one
    @api.constrains('monto_otorgado')
    def _monto_otorgado_check(self):
        saldo_adeudado = self.prestamo_cuenta_id._saldo_adeudado()
        if self.prestamo_cuenta_id.limite_credito < saldo_adeudado + self.monto_otorgado:
            maximo = self.prestamo_cuenta_id.limite_credito - saldo_adeudado
            raise UserError(_("El monto a financiar supera el limite para este cliente. El maximo a financiar es de $%d") % maximo)

    def _monto_total_adeudado(self):
        total = 0
        for cuota in self.prestamo_cuota_ids:
            total += cuota.monto_cuota
        return total

    @api.one
    def actmutuo(self, cr):
        if self.prestamo_plan_id.tipo.prestamo_mutuo_id != False and self.prestamo_plan_id.tipo.prestamo_mutuo_id.content != False:
            self._cargar_mutuo(self.prestamo_plan_id.tipo.prestamo_mutuo_id.content)

    def _cargar_mutuo(self, str_mutuo_text):
        text_default = '________________'
        list_values_remplace = {
            '$compania_nombre$': self.env['res.users'].browse(self.env.uid).company_id.name or text_default,
            '$compania_direccion$': self.env['res.users'].browse(self.env.uid).company_id.street or text_default,
            '$compania_ciudad$': self.env['res.users'].browse(self.env.uid).company_id.city or text_default,
            '$compania_provincia$': self.env['res.users'].browse(self.env.uid).company_id.state_id.name or text_default,
            '$compania_zip$': self.env['res.users'].browse(self.env.uid).company_id.zip or text_default,
            '$compania_pais$': self.env['res.users'].browse(self.env.uid).company_id.country_id.name or text_default,
            '$compania_cuit$': self.env['res.users'].browse(self.env.uid).company_id.company_registry or text_default,
            '$cliente_nombre$': self.prestamo_cuenta_id.cliente_id.name or text_default,
            '$cliente_direccion$': self.prestamo_cuenta_id.cliente_id.street or text_default,
            '$cliente_ciudad$': self.prestamo_cuenta_id.cliente_id.city or text_default,
            '$cliente_provincia$': self.prestamo_cuenta_id.cliente_id.state_id.name or text_default,
            '$cliente_codigo_postal$': self.prestamo_cuenta_id.cliente_id.zip or text_default,
            '$cliente_pais$': self.prestamo_cuenta_id.cliente_id.country_id.name or text_default,
            '$cliente_dni$': self.prestamo_cuenta_id.cliente_dni or text_default,
            '$prestamo_fecha_primer_vencimiento$': str(self.fecha_primer_vencimiento) or text_default,
            '$prestamo_fecha$': str(self.fecha) or text_default,
            '$prestamo_monto_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self.monto_otorgado, 'centavos', 'pesos con ') or text_default,
            '$prestamo_monto$': str(self.monto_otorgado) or text_default,
            '$prestamo_cuotas$': str(self.prestamo_plan_id.cuotas) or text_default,
            '$prestamo_total_adeudado_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self._monto_total_adeudado(), 'centavos', 'pesos con ') or text_default,
            '$prestamo_total_adeudado$': str(self._monto_total_adeudado()) or text_default,
            '$cuota_monto_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self.prestamo_cuota_ids[0].monto_cuota, 'centavos', 'pesos con ') or text_default,
            '$cuota_monto$': str(self.prestamo_cuota_ids[0].monto_cuota) or text_default,
        }

        for value in list_values_remplace:
            str_mutuo_text = str_mutuo_text.replace(value, list_values_remplace[value])
        self.prestamo_mutuo_text = str_mutuo_text

    @api.one
    def actpagare(self, cr):
        if self.prestamo_plan_id.tipo.prestamo_pagare_id != False and self.prestamo_plan_id.tipo.prestamo_pagare_id.content != False:
            self._cargar_pagare(self.prestamo_plan_id.tipo.prestamo_pagare_id.content)

    def _cargar_pagare(self, str_pagare_text):
        str_ret = ""
        text_default = '________________'
        list_values_remplace = {
            '$compania_nombre$': self.env['res.users'].browse(self.env.uid).company_id.name or text_default,
            '$compania_direccion$': self.env['res.users'].browse(self.env.uid).company_id.street or text_default,
            '$compania_ciudad$': self.env['res.users'].browse(self.env.uid).company_id.city or text_default,
            '$compania_provincia$': self.env['res.users'].browse(self.env.uid).company_id.state_id.name or text_default,
            '$compania_zip$': self.env['res.users'].browse(self.env.uid).company_id.zip or text_default,
            '$compania_pais$': self.env['res.users'].browse(self.env.uid).company_id.country_id.name or text_default,
            '$compania_cuit$': self.env['res.users'].browse(self.env.uid).company_id.company_registry or text_default,
            '$cliente_nombre$': self.prestamo_cuenta_id.cliente_id.name or text_default,
            '$cliente_direccion$': self.prestamo_cuenta_id.cliente_id.street or text_default,
            '$cliente_ciudad$': self.prestamo_cuenta_id.cliente_id.city or text_default,
            '$cliente_provincia$': self.prestamo_cuenta_id.cliente_id.state_id.name or text_default,
            '$cliente_codigo_postal$': self.prestamo_cuenta_id.cliente_id.zip or text_default,
            '$cliente_pais$': self.prestamo_cuenta_id.cliente_id.country_id.name or text_default,
            '$cliente_dni$': self.prestamo_cuenta_id.cliente_dni or text_default,
            '$prestamo_fecha_primer_vencimiento$': str(self.fecha_primer_vencimiento) or text_default,
            '$prestamo_fecha$': str(self.fecha) or text_default,
            '$prestamo_monto_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self.monto_otorgado, 'centavos', 'pesos con ') or text_default,
            '$prestamo_monto$': str(self.monto_otorgado) or text_default,
            '$prestamo_cuotas$': str(self.prestamo_plan_id.cuotas) or text_default,
            '$prestamo_total_adeudado_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self._monto_total_adeudado(), 'centavos', 'pesos con ') or text_default,
            '$prestamo_total_adeudado$': str(self._monto_total_adeudado()) or text_default,
            '$cuota_monto_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self.prestamo_cuota_ids[0].monto_cuota, 'centavos', 'pesos con ') or text_default,
            '$cuota_monto$': str(self.prestamo_cuota_ids[0].monto_cuota) or text_default,
        }
        for value in list_values_remplace:
            str_pagare_text = str_pagare_text.replace(value, list_values_remplace[value])
        self.prestamo_pagare_text = str_pagare_text


    @api.multi
    def confirmar(self):
        if self.prestamo_cuota_ids and len(self.prestamo_cuota_ids) >= 1:
            self.state = "confirmado"
            if self.prestamo_plan_id.tipo.prestamo_mutuo_id != False and self.prestamo_plan_id.tipo.prestamo_mutuo_id.content != False:
                self._cargar_mutuo(self.prestamo_plan_id.tipo.prestamo_mutuo_id.content)
            
            if self.prestamo_plan_id.tipo.prestamo_pagare_id != False and self.prestamo_plan_id.tipo.prestamo_pagare_id.content != False:
                self._cargar_pagare(self.prestamo_plan_id.tipo.prestamo_pagare_id.content)

    @api.multi
    def pagar(self):
        self.state = "pagado"
        for cuota in self.prestamo_cuota_ids:
            cuota.state = "activa"

    @api.multi
    def cancelar(self):
        self.state = "cancelado"
        for cuota in self.prestamo_cuota_ids:
            cuota.unlink()

    def caclular_fechas_de_vencimientos(self):
        ret = []
        fecha_primer_vencimiento = self.fecha_primer_vencimiento
        fecha_primer_vencimiento_obj = datetime.strptime(str(fecha_primer_vencimiento), "%Y-%m-%d")
        cantidad_de_cuotas = self.prestamo_plan_id.cuotas
        if self.prestamo_plan_id.dias_entre_vencimientos_select == 'mensual':
            if fecha_primer_vencimiento_obj.day > 28:
                raise ValidationError("Fecha mayor al dia 28 no es correcta.")
            else:
                ret.append(fecha_primer_vencimiento_obj)
                day = fecha_primer_vencimiento_obj.day
                month = fecha_primer_vencimiento_obj.month
                year = fecha_primer_vencimiento_obj.year
                i = 1
                while i < cantidad_de_cuotas:
                    month = month + 1
                    if month > 12:
                        month = 1
                        year = year + 1
                    fecha_str = str(year)+"-"+str(month)+"-"+str(day)
                    fecha_vencimiento = datetime.strptime(str(fecha_str), "%Y-%m-%d")
                    ret.append(fecha_vencimiento)
                    i = i + 1
        else:
            dias_entre_vencimientos = self.prestamo_plan_id.dias_entre_vencimientos
            i = 0
            while i < cantidad_de_cuotas:
                #i = 0, primer cuota ==> dias_totales = 0
                dias_totales = dias_entre_vencimientos * i
                fecha_vencimiento = fecha_primer_vencimiento_obj + timedelta(days=dias_totales)
                ret.append(fecha_vencimiento)
                i = i + 1

        return ret

    def caclular_elementos_cuotas(self):
        ret = []

        capital_total = self.monto_otorgado
        capital_saldo = self.monto_otorgado
        tasa_de_interes_mensual = self.prestamo_plan_id.tasa_de_interes
        periodos = self.prestamo_plan_id.cuotas

        tasa_de_interes_periodo = 0
        dias_periodo = 0
        if self.prestamo_plan_id.dias_entre_vencimientos_select == 'mensual':
            tasa_de_interes_periodo = tasa_de_interes_mensual
            dias_periodo = 30
        elif self.prestamo_plan_id.dias_entre_vencimientos_select == 'quincenal':
            dias_periodo = 15
            tasa_de_interes_periodo = (tasa_de_interes_mensual / 30) * dias_periodo
        elif self.prestamo_plan_id.dias_entre_vencimientos_select == 'semanal':
            dias_periodo = 7
            tasa_de_interes_periodo = (tasa_de_interes_mensual / 30) * dias_periodo
        elif self.prestamo_plan_id.dias_entre_vencimientos_select == 'dias':
            dias_periodo = self.prestamo_plan_id.dias_entre_vencimientos
            tasa_de_interes_periodo = (tasa_de_interes_mensual / 30) * dias_periodo


        #Obtenemos el tax de la compania
        config_account = self.pool.get('account.tax')
        cr = self.env.cr
        uid = self.env.uid
        config_account_id = config_account.search(cr, uid, [('type_tax_use', '=', 'sale'), ('amount', '>', 0)])
        for conf in config_account_id:
            conf_obj = config_account.browse(cr, uid, conf, context=None)

        if self.prestamo_plan_id.tipo_de_amortizacion == 'sistema_directa':
            #Calculamos el capital de la cuota - igual para todas las cuotas
            capital_cuota = float("{0:.2f}".format(capital_total / periodos))
            diferencia_centavos = float("{0:.2f}".format((capital_cuota * periodos) - capital_total))
            
            #Calculamos el interes - igual para todas las cuotas

            if self.prestamo_plan_id.iva_incluido:
                iva = 1 + (self.prestamo_plan_id.iva_id.amount / 100)
                interes_cuota = float("{0:.2f}".format((((tasa_de_interes_periodo * periodos) * capital_total) / periodos) / iva))
            else:
                interes_cuota = float("{0:.2f}".format(((tasa_de_interes_periodo * periodos) * capital_total) / periodos))

            interes_adicional_cuota = 0
            iva_adicional_cuota = 0
            i = 0
            while i < periodos:
                #Calculamos la diferencia en centavos del capital_cuota de la primer cuota
                if i == 0:
                    capital_cuota = capital_cuota - diferencia_centavos
                    
                    #Calculamos el posible interes adicional de la primer cuota
                    fecha_inicial = datetime.strptime(str(self.fecha), "%Y-%m-%d")
                    fecha_final = datetime.strptime(str(self.fecha_primer_vencimiento), "%Y-%m-%d")
                    diferencia = fecha_final - fecha_inicial
                    dias_adicionales_primer_cuota = diferencia.days - dias_periodo
                    if dias_adicionales_primer_cuota > 0:
                        interes_adicional_cuota = (float("{0:.2f}".format(dias_adicionales_primer_cuota)) / float("{0:.2f}".format(dias_periodo))) * interes_cuota
                        iva_adicional_cuota = interes_adicional_cuota * (self.prestamo_plan_id.iva_id.amount / 100)
                else:
                    capital_cuota = float("{0:.2f}".format(capital_total / periodos))
                    interes_adicional_cuota = 0
                    iva_adicional_cuota = 0

                #Calculamos el capital_saldo - disminuye conforme avanzan las cuotas
                capital_saldo = capital_total - capital_cuota * i

                #Calculamos el iva cuota - sobre el interes - igual para todas las cuotas
                iva_cuota = float("{0:.2f}".format(interes_cuota * (self.prestamo_plan_id.iva_id.amount / 100)))
                ret.append((capital_saldo, capital_cuota, interes_cuota+interes_adicional_cuota, iva_cuota+iva_adicional_cuota))
                i = i + 1

        return ret


    @api.multi
    def calcular_cuotas_plan(self):

        if len(self.prestamo_cuota_ids) > 0:
            for cuota in self.prestamo_cuota_ids:
                cuota.unlink()


        if self.prestamo_plan_id.cuotas != False:
            cuota_ids = []
            i = 0            
            fecha_valores = self.caclular_fechas_de_vencimientos()
            elementos_cuotas = self.caclular_elementos_cuotas()
            while i < self.prestamo_plan_id.cuotas:
                fecha_vencimiento = fecha_valores[i]
                ec = elementos_cuotas[i]
                capital_saldo = ec[0]
                capital_cuota = ec[1]
                interes_cuota = ec[2]
                iva_cuota = ec[3]
                monto_cuota = capital_cuota + interes_cuota + iva_cuota
                numero_cuota = i + 1

                val = {
                    'numero_cuota': numero_cuota,
                    'fecha_vencimiento': fecha_vencimiento,
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

                prestamo_cuota_n = self.env['prestamo.cuota'].create(val)
                cuota_ids.append(prestamo_cuota_n.id)
                i = i + 1
            self.prestamo_cuota_ids = cuota_ids


class prestamo_obseracion(osv.Model):
    _name = 'prestamo.observacion'
    _description = 'Informacion de observaciones'
    _order = "id desc"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'detalle': fields.char("Lo conversado", required=True),
        'fecha_proxima_accion': fields.date("Fecha para la proxima accion"),
        'accion': fields.char("Accion a realizar"),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
    }
    _defaults = {
        'fecha': lambda *a: time.strftime('%Y-%m-%d'),
    }

class prestamo_pago(osv.Model):
    _name = 'prestamo.pago'
    _description = 'Informacion del pago del prestamo'
    _rec_name="name"
    _order = "id desc"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'name': fields.char("ID", readonly=True, compute="compute_name"),
        'fecha': fields.date("Fecha", required=True),
        'monto': fields.float("Capital otorgado", readonly=True),
        'journal_id': fields.many2one('account.journal', string="Metodo de Pago", required=True, domain="[('type', 'in', ('bank', 'cash'))]"),
        'prestamo_prestamo_id': fields.many2one("prestamo.prestamo", "Prestamo"),
        'comision_de_apertura': fields.float("Comision de apertura (%)", help="Aplicado sobre monto otorgado.", digits=(16,3)),
        'monto_de_apertura': fields.float("", readonly=True, compute="compute_monto_de_apertura"),
        'gastos_de_gestion': fields.float("Gaston de gestion", help="Es el monto de gastos de gestion, el cual disminuye el monto otorgado al cliente."),
        'monto_recibido': fields.float("Neto", readonly=True, compute="compute_monto_recibido"),
        'move_id': fields.many2one("account.move", "Asiento", readonly=True),
        'state': fields.selection([('borrador', 'Borrador'), ('confirmado', 'Confirmado')], string='Estado', readonly=True),
    }

    @api.one
    @api.depends('journal_id')
    def compute_name(self):
        self.name = "Comprobante/" + self.journal_id.name + "/" + str(self.id)

    @api.one
    @api.depends('comision_de_apertura')
    def compute_monto_de_apertura(self):
        self.monto_de_apertura = self.monto * self.comision_de_apertura

    @api.one
    @api.depends('comision_de_apertura', 'gastos_de_gestion')
    def compute_monto_recibido(self):
        self.monto_recibido = self.monto - self.monto_de_apertura - self.gastos_de_gestion

    _defaults = {
        'fecha': lambda *a: time.strftime('%Y-%m-%d'),
        'state': 'borrador',
    }

    @api.model
    def default_get(self, fields):
        rec = super(prestamo_pago, self).default_get(fields)
        context = dict(self._context or {})
        active_model = context.get('active_model')
        active_ids = context.get('active_ids')
        active_id = context.get('active_id')

        # Checks on context parameters
        if not active_model or not active_ids:
            raise UserError(_("Programmation error: wizard action executed without active_model or active_ids in context."))
        if active_model != 'prestamo.prestamo':
            raise UserError(_("Programmation error: the expected model for this action is 'prestamo.prestamo'. The provided one is '%d'.") % active_model)

        # Checks on received cuotas records
        prestamo = self.env[active_model].browse(active_id)
        total_amount = prestamo[0].monto_otorgado
        comision_de_apertura = prestamo[0].prestamo_plan_id.comision_de_apertura
        monto_de_apertura = prestamo[0].prestamo_plan_id.comision_de_apertura * total_amount
        gastos_de_gestion = prestamo[0].prestamo_plan_id.gastos_de_gestion
        rec.update({
            'monto': abs(total_amount),
            'prestamo_prestamo_id': active_id,
            'comision_de_apertura': comision_de_apertura,
            'monto_de_apertura': monto_de_apertura,
            'gastos_de_gestion': gastos_de_gestion,
        })
        return rec

    def _get_prestamo(self):
        return self.env['prestamo.prestamo'].browse(self._context.get('active_id'))[0]


    def crear_move_pago(self):
        move = None
        company_id = self.env['res.users'].browse(self.env.uid).company_id.id

        if True:
            #list of move line
            line_ids = []
            # create move line
            # Registro el monto pagado
            aml = {
                'date': self.fecha,
                'account_id': self.journal_id.default_debit_account_id.id,
                'name': 'Prestamo - Pago prestamo',
                'partner_id': self.prestamo_prestamo_id.prestamo_cuenta_id.cliente_id.id,
                'credit': self.monto,
            }
            line_ids.append((0,0,aml))

            # create move line
            # Acredito el monto a la cuenta del cliente
            aml2 = {
                'date': self.fecha,
                'account_id': self.prestamo_prestamo_id.prestamo_cuenta_id.cliente_id.property_account_receivable_id.id,
                'name': 'Prestamo - Pago prestamo',
                'partner_id': self.prestamo_prestamo_id.prestamo_cuenta_id.cliente_id.id,
                'debit': self.monto,
            }
            line_ids.append((0,0,aml2))

            costos_de_otorgamiento = self.monto_de_apertura + self.gastos_de_gestion

            # create move line
            # Debito la comision de apertura mas gastos a la cuenta desde donde se efectua el pago
            aml3 = {
                'date': self.fecha,
                'account_id': self.journal_id.default_debit_account_id.id,
                'name': 'Prestamo - Cobro gastos y comisiones de otorgamiento',
                'partner_id': self.prestamo_prestamo_id.prestamo_cuenta_id.cliente_id.id,
                'debit': costos_de_otorgamiento,
            }
            line_ids.append((0,0,aml3))

            # create move line
            # Acredito la comision de apertura mas gastos en cuenta ganancia
            aml4 = {
                'date': self.fecha,
                'account_id': self.prestamo_prestamo_id.prestamo_plan_id.journal_otros_ingresos_id.default_debit_account_id.id,
                'name': 'Prestamo - Cobro gastos y comisiones de otorgamiento',
                'partner_id': self.prestamo_prestamo_id.prestamo_cuenta_id.cliente_id.id,
                'credit': costos_de_otorgamiento,
            }
            line_ids.append((0,0,aml4))

            move_name = "Prestamo/Pago"
            move = self.env['account.move'].create({
                'name': move_name,
                'date': self.fecha,
                'journal_id': self.journal_id.id,
                'state':'draft',
                'company_id': company_id,
                'partner_id': self.prestamo_prestamo_id.prestamo_cuenta_id.cliente_id.id,
                'line_ids': line_ids,
            })
            move.state = 'posted'
            self.move_id = move.id

        return move

    @api.one
    def crear_pago(self):
        monto = self.monto
        prestamo = self._get_prestamo()
        if prestamo.monto_otorgado == self.monto:
            prestamo.pagar()
            prestamo.prestamo_pago_id = self.id
            self.crear_move_pago()
            self.state = 'confirmado'
        else:
            raise ValidationError("El monto no coincide con el prestamo")
        return {'type': 'ir.actions.act_window_close'}

class prestamo_recibo(osv.Model):
    _name = 'prestamo.recibo'
    _description = 'Informacion de recibos'
    _rec_name="id"
    _order = "id desc"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'fecha': fields.date("Fecha", required=True),
        'monto': fields.float("Monto", required=True),
        'journal_id': fields.many2one('account.journal', string="Metodo de Cobro", required=True, domain="[('type', 'in', ('bank', 'cash'))]"),
        'prestamo_cuota_ids': fields.one2many("prestamo.cuota", 'prestamo_recibo_id',"Cuota"),
        'prestamo_comprobante_de_pago_text': fields.html("Comprobante"),
        'detalle': fields.text("Detalle", readonly=True),
        'prestamo_cuenta_id': fields.many2one("prestamo.cuenta", "Cuenta", required=True),
        'invoice':fields.boolean('Emitir factura'),
        'invoice_id':fields.many2one('account.invoice', 'Factura', readonly=True),
        'iva_id': fields.many2one("account.tax", "Impuesto", domain="[('active', '=', True), ('type_tax_use', '=', 'sale')]"),
        'move_id': fields.many2one("account.move", "Asiento", readonly=True),
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
        active_id = context.get('active_id')

        # Checks on context parameters
        if not active_model or not active_ids:
            raise UserError(_("Programmation error: wizard action executed without active_model or active_ids in context."))
        if active_model != 'prestamo.cuota':
            raise UserError(_("Programmation error: the expected model for this action is 'account.invoice'. The provided one is '%d'.") % active_model)

        # Checks on received cuotas records
        cuotas = self.env[active_model].browse(active_ids)

        total_amount = sum(cuota.saldo_cuota for cuota in cuotas)
        rec.update({
            'monto': float("{0:.2f}".format(abs(total_amount))),
            'prestamo_cuenta_id': cuotas[0].prestamo_prestamo_id.prestamo_cuenta_id.id,
            'invoice': cuotas[0].prestamo_prestamo_id.prestamo_plan_id.invoice,
            'iva_id': cuotas[0].prestamo_prestamo_id.prestamo_plan_id.iva_id.id,
        })
        return rec

    def _get_cuotas(self):
        return self.env['prestamo.cuota'].browse(self._context.get('active_ids'))

    def get_recibo_vals(self):
        """ Hook for extension """
        return {
            'fecha': self.fecha,
            'monto': self.monto,
            'journal_id': self.journal_id.id,
            'prestamo_cuota_ids': [(4, cuota.id, None) for cuota in self._get_cuotas()],
            'prestamo_cuenta_id': self.prestamo_cuenta_id.id,
        }


    def crear_move_cobro(self, prestamo, capital, interes, iva, punitorios):
        move = None
        company_id = self.env['res.users'].browse(self.env.uid).company_id.id
        if True:
            #list of move line
            line_ids = []
            # create move line
            # Registro el monto cobrado en caja
            aml = {
                'date': self.fecha,
                'account_id': self.journal_id.default_debit_account_id.id,
                'name': 'Prestamo - Cuotas cobradas',
                'partner_id': self.prestamo_cuenta_id.cliente_id.id,
                'debit': self.monto,
            }
            line_ids.append((0,0,aml))

            # create move line
            # Acredito el devolucion capital a la cuenta del cliente
            aml2 = {
                'date': self.fecha,
                'account_id': self.prestamo_cuenta_id.cliente_id.property_account_receivable_id.id,
                'name': 'Prestamo - Devolucion capital',
                'partner_id': self.prestamo_cuenta_id.cliente_id.id,
                'credit': capital,
            }
            line_ids.append((0,0,aml2))

            # create move line
            # Acredito el IVA a pagar - cuenta a pagar
            aml3 = {
                'date': self.fecha,
                'account_id': prestamo.prestamo_plan_id.iva_id.account_id.id,
                'name': 'Prestamo - IVA cobrado',
                'partner_id': self.prestamo_cuenta_id.cliente_id.id,
                'credit': iva,
            }
            line_ids.append((0,0,aml3))

            # create move line
            # Acredito el la ganancias de intereses
            intereses_cobrados = interes + punitorios
            aml4 = {
                'date': self.fecha,
                'account_id': prestamo.prestamo_plan_id.journal_id.default_debit_account_id.id,
                'name': 'Prestamo - Intereses cobrados',
                'partner_id': self.prestamo_cuenta_id.cliente_id.id,
                'credit': intereses_cobrados,
            }
            line_ids.append((0,0,aml4))

            move_name = "Prestamo/Cobro/" + str(self.id)
            move = self.env['account.move'].create({
                'name': move_name,
                'date': self.fecha,
                'journal_id': self.journal_id.id,
                'state':'draft',
                'company_id': company_id,
                'partner_id': self.prestamo_cuenta_id.cliente_id.id,
                'line_ids': line_ids,
            })
            move.state = 'posted'
            self.move_id = move.id

            if self.invoice:

                account_invoice_obj = self.env['account.invoice']
                # Create invoice line
                ail = {
                    'name': "Intereses por prestamo.",
                    'quantity':1,
                    'price_unit': intereses_cobrados,
                    'account_id': prestamo.prestamo_plan_id.journal_id.default_debit_account_id.id,
                    'invoice_line_tax_ids':[(6,0,[self.iva_id.id])],
                }

                account_invoice_customer0 = account_invoice_obj.sudo(self.env.uid).create(dict(
                    name=move_name,
                    date=self.fecha,
                    reference_type="none",
                    type="out_invoice",
                    reference=False,
                    #payment_term_id=self.payment_term.id,
                    journal_id=prestamo.prestamo_plan_id.journal_id.id,
                    partner_id=self.prestamo_cuenta_id.cliente_id.id,
                    move_id=move.id,
                    #residual=self.gasto_interes_liquidacion,
                    #residual_company_signed=self.gasto_interes_liquidacion,
                    #residual_signed=self.gasto_interes_liquidacion,
                    account_id=self.journal_id.id,
                    invoice_line_ids=[(0, 0, ail)]
                ))
                account_invoice_customer0.signal_workflow('invoice_open')
                #account_invoice_customer0.reconciled = True
                account_invoice_customer0.state = 'paid'
                self.invoice_id = account_invoice_customer0.id

        return move

    @api.one
    def crear_recibo(self):
        #recibo = self.env['prestamo.recibo'].create(self.get_recibo_vals())
        #self.prestamo_cuota_ids = [(4, cuota.id, None) for cuota in self._get_cuotas()]
        cuotas_cobradas = []
        monto = self.monto
        capital = 0
        interes = 0
        punitorios = 0
        iva = 0
        val = None
        detalle = ""
        for cuota in self._get_cuotas():
            prestamo = cuota.prestamo_prestamo_id
            if monto > 0:
                val = cuota.get_conceptos_de_cobro(monto)
                capital = float("{0:.2f}".format(capital + val['capital']))
                interes = float("{0:.2f}".format(interes + val['interes']))
                iva = float("{0:.2f}".format(iva + val['iva']))
                punitorios = float("{0:.2f}".format(punitorios + val['punitorios']))

                cuotas_cobradas.append((4, cuota.id, None))
                cuota.ultima_fecha_cobro_cuota = self.fecha
                resto = float("{0:.2f}".format(monto - cuota.saldo_cuota))
                if resto >= 0:
                    if cuota.cobrado_cuota > 0:

                        detalle = detalle + "Cobro (final) de $" + str(cuota.saldo_cuota) + " en concepto de saldo de cuota Nro " + str(cuota.numero_cuota) + ", " + cuota.prestamo_prestamo_id.display_name + ". "
                    else:
                        detalle = detalle + "Cobro (total) de $" + str(cuota.saldo_cuota) + " en concepto de cuota Nro " + str(cuota.numero_cuota) + ", " + cuota.prestamo_prestamo_id.display_name + ". "
                    cuota.cobrado_cuota = abs(cuota.cobrado_cuota + cuota.saldo_cuota)
                    cuota.state = 'cobrada'
                    #if cuota.saldo_cuota != 0:
                    #    raise UserError(_("Cuota no cobrada en su totalidad."))
                else:
                    cuota.cobrado_cuota = abs(cuota.cobrado_cuota + monto)
                    detalle = detalle + "Cobro (parcial) de $" + str(monto) + " en concepto de cuota Nro " + str(cuota.numero_cuota) + ", " + cuota.prestamo_prestamo_id.display_name + ". "
                monto = resto
        self.prestamo_cuota_ids = cuotas_cobradas
        self.detalle = detalle
        self.crear_move_cobro(prestamo, capital, interes, iva, punitorios)
        if self.prestamo_cuota_ids[0].prestamo_prestamo_id.prestamo_plan_id.tipo.prestamo_cobro_cuota_id != False:
                self._cargar_comprobante(self.prestamo_cuota_ids[0].prestamo_prestamo_id.prestamo_plan_id.tipo.prestamo_cobro_cuota_id.content)

        return {'type': 'ir.actions.act_window_close'}

    @api.one
    def webon(self, cr):
        if self.prestamo_cuota_ids[0].prestamo_prestamo_id.prestamo_plan_id.tipo.prestamo_cobro_cuota_id != False and self.prestamo_cuota_ids[0].prestamo_prestamo_id.prestamo_plan_id.tipo.prestamo_cobro_cuota_id.content != False:
                self._cargar_comprobante(self.prestamo_cuota_ids[0].prestamo_prestamo_id.prestamo_plan_id.tipo.prestamo_cobro_cuota_id.content)

    def _cargar_comprobante(self, str_comprobante_text):
        str_ret = ""
        text_default = '________________'
        list_values_remplace = {
            '$compania_nombre$': self.env['res.users'].browse(self.env.uid).company_id.name or text_default,
            '$compania_direccion$': self.env['res.users'].browse(self.env.uid).company_id.street or text_default,
            '$compania_ciudad$': self.env['res.users'].browse(self.env.uid).company_id.city or text_default,
            '$compania_provincia$': self.env['res.users'].browse(self.env.uid).company_id.state_id.name or text_default,
            '$compania_zip$': self.env['res.users'].browse(self.env.uid).company_id.zip or text_default,
            '$compania_pais$': self.env['res.users'].browse(self.env.uid).company_id.country_id.name or text_default,
            '$compania_cuit$': self.env['res.users'].browse(self.env.uid).company_id.company_registry or text_default,
            '$cliente_nombre$': self.prestamo_cuenta_id.cliente_id.name or text_default,
            '$cliente_direccion$': self.prestamo_cuenta_id.cliente_id.street or text_default,
            '$cliente_ciudad$': self.prestamo_cuenta_id.cliente_id.city or text_default,
            '$cliente_provincia$': self.prestamo_cuenta_id.cliente_id.state_id.name or text_default,
            '$cliente_codigo_postal$': self.prestamo_cuenta_id.cliente_id.zip or text_default,
            '$cliente_pais$': self.prestamo_cuenta_id.cliente_id.country_id.name or text_default,
            '$cliente_dni$': self.prestamo_cuenta_id.cliente_dni or text_default,
            '$monto_pagado$': str(self.monto) or text_default,
            '$monto_pagado_en_letras$': amount_to_text_es_MX.get_amount_to_text(self, self.monto, 'centavos', 'pesos con ') or text_default,
            '$detalle$': str(self.detalle) or text_default,
            '$numero_de_recibo$': str(self.id) or text_default,
        }
        for value in list_values_remplace:
            str_comprobante_text = str_comprobante_text.replace(value, list_values_remplace[value])
        self.prestamo_comprobante_de_pago_text = str_comprobante_text


class prestamo_cuenta(osv.Model):
    _name = 'prestamo.cuenta'
    _description = 'Detalles de la cuenta de una cliente'
    _rec_name = "cliente_id"
    _columns = {
        'id': fields.integer("ID", readonly=True),
        'cliente_id': fields.many2one("res.partner", "Cliente", required=True),
        'cliente_dni': fields.char("DNI", required=True),
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


    def _saldo_adeudado(self):
        saldo_adeudado = 0
        for prestamo in self.prestamo_prestamo_ids:
            for cuota in prestamo.prestamo_cuota_ids:
                if cuota.state != 'cobrada':
                    saldo_adeudado += cuota.saldo_cuota
        return saldo_adeudado

    def confirmar(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, {'state':'confirmado'}, context=None)
        return True

    _sql_constraints = [
        ('id_uniq', 'unique (id)', "El ID ya existe!"),
        ('cliente_id_uniq', 'unique (cliente_id)', "El cliente ya tiene una cuenta asociada!"),
    ]
    _defaults = {
        'fecha_inicio_trabajo_actual': lambda *a: time.strftime('%Y-%m-%d'),
        'state': "borrador",
        'active': True,
    }

class prestamo_documentacion(osv.Model):
    _name = 'prestamo.documentacion'
    _description = 'Documentos relacionados a prestamos'
    _columns = {
        'name': fields.char('Nombre', required=True),
        'values': fields.char('Valores a reemplazar', readonly=True),
        'content': fields.html('Contenido'),
    }
    _defaults = {
        'values': "$compania_nombre,$compania_ciudad,$compania_calle,$cliente_nombre,$cliente_direccion,$prestamo_fecha,$prestamo_monto,$prestamo_monto_en_letras,$prestamo_cuotas,$prestamo_fecha_primer_vencimiento,$prestamo_total_adeudado,$prestamo_total_adeudado_en_letras,$cuota_monto,cuota_monto_en_letras",
    }
