"""Compatibility shim for the old Architect role.

Deprecated: use agents.roles.tech_lead_agent instead.
"""

from agents.roles.tech_lead_agent import TechLeadAgent as ArchitectAgent
from agents.roles.tech_lead_agent import TechLeadDeliberatingAgent as ArchitectDeliberatingAgent
