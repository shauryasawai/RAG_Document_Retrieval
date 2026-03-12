from django import template
register = template.Library()

@register.filter
def has_answer(question):
    return hasattr(question, 'answer')
