"""Select a renderer at the HTML boundary. Both editions use the same APIs and database."""
import os

from flask import has_request_context, render_template, request


def classic_ui():
    selected = request.cookies.get('jmconsole_edition') if has_request_context() else None
    if selected not in ('classic', 'atelier'):
        selected = os.environ.get('JMCONSOLE_UI', 'atelier')
    return selected == 'classic'


def render_page(name, **context):
    return render_template(('classic/' if classic_ui() else '') + name, **context)
