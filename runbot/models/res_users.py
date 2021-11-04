
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    runbot_team_id = fields.Many2one('runbot.build.error.team', "Runbot Team")
