# -*- coding: utf-8 -*-
import hashlib
import logging
import re

from collections import defaultdict
from fnmatch import fnmatch
from odoo import models, fields, api
from odoo.exceptions import ValidationError
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


class BuildError(models.Model):

    _name = "runbot.build.error"
    _description = "Build error"

    _inherit = "mail.thread"
    _rec_name = "id"

    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
    summary = fields.Char('Content summary', compute='_compute_summary', store=False)
    module_name = fields.Char('Module name')  # name in ir_logging
    function = fields.Char('Function name')  # func name in ir logging
    fingerprint = fields.Char('Error fingerprint', index=True)
    random = fields.Boolean('underterministic error', tracking=True)
    responsible = fields.Many2one('res.users', 'Assigned fixer', tracking=True)
    team_id = fields.Many2one('runbot.team', 'Assigned team')
    fixing_commit = fields.Char('Fixing commit', tracking=True)
    fixing_pr = fields.Char('Fixing PR', tracking=True)
    build_ids = fields.Many2many('runbot.build', 'runbot_build_error_ids_runbot_build_rel', string='Affected builds')
    bundle_ids = fields.One2many('runbot.bundle', compute='_compute_bundle_ids')
    version_ids = fields.One2many('runbot.version', compute='_compute_version_ids', string='Versions', search='_search_version')
    trigger_ids = fields.Many2many('runbot.trigger', compute='_compute_trigger_ids')
    active = fields.Boolean('Error is not fixed', default=True, tracking=True)
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags')
    build_count = fields.Integer(compute='_compute_build_counts', string='Nb seen', store=True)
    parent_id = fields.Many2one('runbot.build.error', 'Linked to', index=True)
    child_ids = fields.One2many('runbot.build.error', 'parent_id', string='Child Errors', context={'active_test': False})
    children_build_ids = fields.Many2many('runbot.build', compute='_compute_children_build_ids', string='Children builds')
    error_history_ids = fields.Many2many('runbot.build.error', compute='_compute_error_history_ids', string='Old errors', context={'active_test': False})
    first_seen_build_id = fields.Many2one('runbot.build', compute='_compute_first_seen_build_id', string='First Seen build')
    first_seen_date = fields.Datetime(string='First Seen Date', related='first_seen_build_id.create_date')
    last_seen_build_id = fields.Many2one('runbot.build', compute='_compute_last_seen_build_id', string='Last Seen build', store=True)
    last_seen_date = fields.Datetime(string='Last Seen Date', related='last_seen_build_id.create_date', store=True)
    test_tags = fields.Char(string='Test tags', help="Comma separated list of test_tags to use to reproduce/remove this error")

    @api.constrains('test_tags')
    def _check_test_tags(self):
        for build_error in self:
            if build_error.test_tags and '-' in build_error.test_tags:
                raise ValidationError('Build error test_tags should not be negated')

    @api.model_create_single
    def create(self, vals):
        cleaners = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        content = vals.get('content')
        cleaned_content = cleaners.r_sub('%', content)
        vals.update({'cleaned_content': cleaned_content,
                     'fingerprint': self._digest(cleaned_content)
        })
        if not 'team_id' in vals and 'module_name' in vals:
            vals.update({'team_id': self.env['runbot.team']._get_team(vals['module_name'])})
        return super().create(vals)

    def write(self, vals):
        if 'active' in vals:
            for build_error in self:
                (build_error.child_ids - self).write({'active': vals['active']})
        return super(BuildError, self).write(vals)

    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_build_counts(self):
        for build_error in self:
            build_error.build_count = len(build_error.build_ids | build_error.mapped('child_ids.build_ids'))

    @api.depends('build_ids')
    def _compute_bundle_ids(self):
        for build_error in self:
            top_parent_builds = build_error.build_ids.mapped(lambda rec: rec and rec.top_parent)
            build_error.bundle_ids = top_parent_builds.mapped('slot_ids').mapped('batch_id.bundle_id')

    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_version_ids(self):
        for build_error in self:
            build_error.version_ids = build_error.build_ids.version_id

    @api.depends('build_ids')
    def _compute_trigger_ids(self):
        for build_error in self:
            build_error.trigger_ids = build_error.mapped('build_ids.params_id.trigger_id')

    @api.depends('content')
    def _compute_summary(self):
        for build_error in self:
            build_error.summary = build_error.content[:50]

    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_children_build_ids(self):
        for build_error in self:
            all_builds = build_error.build_ids | build_error.mapped('child_ids.build_ids')
            build_error.children_build_ids = all_builds.sorted(key=lambda rec: rec.id, reverse=True)

    @api.depends('children_build_ids')
    def _compute_last_seen_build_id(self):
        for build_error in self:
            build_error.last_seen_build_id = build_error.children_build_ids and build_error.children_build_ids[0] or False

    @api.depends('children_build_ids')
    def _compute_first_seen_build_id(self):
        for build_error in self:
            build_error.first_seen_build_id = build_error.children_build_ids and build_error.children_build_ids[-1] or False

    @api.depends('fingerprint', 'child_ids.fingerprint')
    def _compute_error_history_ids(self):
        for error in self:
            fingerprints = [error.fingerprint] + [rec.fingerprint for rec in error.child_ids]
            error.error_history_ids = self.search([('fingerprint', 'in', fingerprints), ('active', '=', False), ('id', '!=', error.id or False)])

    @api.model
    def _digest(self, s):
        """
        return a hash 256 digest of the string s
        """
        return hashlib.sha256(s.encode()).hexdigest()

    @api.model
    def _known(self, log_message):
        regexes = self.env['runbot.error.regex'].search([])
        cleaning_regs = regexes.filtered(lambda r: r.re_type == 'cleaning')
        fingerprint = self._digest(cleaning_regs.r_sub('%', log_message))
        return self.env['runbot.build.error'].search([('fingerprint', '=', fingerprint), ('active', '=', True)])

    @api.model
    def _parse_logs(self, ir_logs):

        regexes = self.env['runbot.error.regex'].search([])
        search_regs = regexes.filtered(lambda r: r.re_type == 'filter')
        cleaning_regs = regexes.filtered(lambda r: r.re_type == 'cleaning')

        hash_dict = defaultdict(list)
        for log in ir_logs:
            if search_regs.r_search(log.message):
                continue
            fingerprint = self._digest(cleaning_regs.r_sub('%', log.message))
            hash_dict[fingerprint].append(log)

        build_errors = self.env['runbot.build.error']
        # add build ids to already detected errors
        existing_errors = self.env['runbot.build.error'].search([('fingerprint', 'in', list(hash_dict.keys())), ('active', '=', True)])
        build_errors |= existing_errors
        for build_error in existing_errors:
            for build in {rec.build_id for rec in hash_dict[build_error.fingerprint]}:
                build.build_error_ids += build_error
            del hash_dict[build_error.fingerprint]

        # create an error for the remaining entries
        for fingerprint, logs in hash_dict.items():
            build_errors |= self.env['runbot.build.error'].create({
                'content': logs[0].message,
                'module_name': logs[0].name,
                'function': logs[0].func,
                'build_ids': [(6, False, [r.build_id.id for r in logs])],
            })

        if build_errors:
            window_action = {
                "type": "ir.actions.act_window",
                "res_model": "runbot.build.error",
                "views": [[False, "tree"]],
                "domain": [('id', 'in', build_errors.ids)]
            }
            if len(build_errors) == 1:
                window_action["views"] = [[False, "form"]]
                window_action["res_id"] = build_errors.id
            return window_action

    def link_errors(self):
        """ Link errors with the first one of the recordset
        choosing parent in error with responsible, random bug and finally fisrt seen
        """
        if len(self) < 2:
            return
        self = self.with_context(active_test=False)
        build_errors = self.search([('id', 'in', self.ids)], order='responsible asc, random desc, id asc')
        build_errors[1:].write({'parent_id': build_errors[0].id})

    def clean_content(self):
        cleaning_regs = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        for build_error in self:
            build_error.cleaned_content = cleaning_regs.r_sub('%', build_error.content)

    @api.model
    def test_tags_list(self):
        active_errors = self.search([('test_tags', '!=', False)])
        test_tag_list = active_errors.mapped('test_tags')
        return [test_tag for error_tags in test_tag_list for test_tag in (error_tags).split(',')]

    @api.model
    def disabling_tags(self):
        return ['-%s' % tag for tag in self.test_tags_list()]

    def _search_version(self, operator, value):
        return [('build_ids.version_id', operator, value)]


class BuildErrorTag(models.Model):

    _name = "runbot.build.error.tag"
    _description = "Build error tag"

    name = fields.Char('Tag')
    error_ids = fields.Many2many('runbot.build.error', string='Errors')


class ErrorRegex(models.Model):

    _name = "runbot.error.regex"
    _description = "Build error regex"
    _inherit = "mail.thread"
    _rec_name = 'id'
    _order = 'sequence, id'

    regex = fields.Char('Regular expression')
    re_type = fields.Selection([('filter', 'Filter out'), ('cleaning', 'Cleaning')], string="Regex type")
    sequence = fields.Integer('Sequence', default=100)

    def r_sub(self, replace, s):
        """ replaces patterns from the recordset by replace in the given string """
        for c in self:
            s = re.sub(c.regex, '%', s)
        return s

    def r_search(self, s):
        """ Return True if one of the regex is found in s """
        for filter in self:
            if re.search(filter.regex, s):
                return True
        return False


class BuildErrorTeam(models.Model):

    _name = 'runbot.team'
    _description = "Runbot Team"

    name = fields.Char('Team')
    user_ids = fields.One2many('res.users', 'runbot_team_id', domain=[('share', '=', False)])
    build_error_ids = fields.One2many('runbot.build.error', 'team_id', string='Team Errors')
    module_wildcards = fields.Char('Module Wildcards',
        help='Comma separated list of `fnmatch` wildcards\n'
        'Negative wildcards starting with a `-` can be used to discard some modules\n'
        'e.g.: `*website*,-*website_sale*`')
    dashboard_ids = fields.One2many('runbot.team.dashboard', 'team_id', string='Dashboards')

    @api.model
    def _get_team(self, module_name):
        for team in self.env['runbot.team'].search([('module_wildcards', '!=', False)]):
            match = any([fnmatch(module_name, pattern.strip()) for pattern in team.module_wildcards.split(',') if not pattern.strip().startswith('-')])
            unmatch = any([fnmatch(module_name, pattern.strip().strip('-')) for pattern in team.module_wildcards.split(',') if pattern.strip().startswith('-')])
            if match and not unmatch:
                return team.id
        return False


class BuildErrorTeamDashboard(models.Model):

    _name = 'runbot.team.dashboard'
    _description = "Runbot Team Dashboard"

    display_name = fields.Char(compute='_compute_display_name')
    team_id = fields.Many2one('runbot.team', 'Team')
    project_id = fields.Many2one('runbot.project', 'Project', help='Project to monitor', required=True, default=lambda self: self.env.ref('runbot.main_project'))
    category_id = fields.Many2one('runbot.category', 'Category', help='Trigger Category to monitor', required=True)
    trigger_id = fields.Many2one('runbot.trigger', 'Trigger', help='Trigger to monitor in chosen category')
    config_id = fields.Many2one('runbot.build.config', 'Config', help='Select a sub_build with this config')
    check_sub_builds = fields.Boolean('Check Sub Builds', default=False, help='Check the sub_builds for the results')
    domain_filter = fields.Char('Domain Filter', help='If present, will be applied on builds', default="[('global_result', '=', 'ko')]")
    sticky_bundle_ids = fields.Many2many('runbot.bundle', compute='_compute_sticky_bundle_ids', string='Sticky Bundles')
    build_ids = fields.Many2many('runbot.build', compute='_compute_build_ids', string='Builds')

    @api.depends('project_id', 'category_id', 'trigger_id', 'config_id')
    def _compute_display_name(self):
        for board in self:
            names = [board.project_id.name, board.category_id.name,board.trigger_id.name, board.config_id.name]
            board.display_name = ' / '.join([n for n in names if n])

    @api.depends('project_id')
    def _compute_sticky_bundle_ids(self):
        sticky_bundles = self.env['runbot.bundle'].search([('sticky', '=', True)])
        for dashboard in self:
            dashboard.sticky_bundle_ids = sticky_bundles.filtered(lambda b: b.project_id == dashboard.project_id)

    @api.depends('project_id', 'category_id', 'trigger_id', 'config_id', 'domain_filter')
    def _compute_build_ids(self):
        default_category_id = self.env['ir.model.data'].xmlid_to_res_id('runbot.default_category')
        for dashboard in self:
            category_id = dashboard.category_id.id or default_category_id
            last_done_batch_ids = dashboard.sticky_bundle_ids.with_context(category_id=category_id).last_done_batch
            if dashboard.trigger_id:
                all_build_ids = last_done_batch_ids.slot_ids.filtered(lambda s: s.trigger_id == dashboard.trigger_id).all_build_ids
            else:
                all_build_ids = last_done_batch_ids.all_build_ids

            domain = [('global_result', '=', 'ko')]
            if dashboard.config_id:
                domain.append(('config_id', '=', dashboard.config_id.id))
            builds = all_build_ids.filtered_domain(domain)
            if dashboard.check_sub_builds:
                builds = builds.children_ids
            if dashboard.domain_filter:
                builds = builds.filtered_domain(safe_eval(dashboard.domain_filter))
            dashboard.build_ids = builds
