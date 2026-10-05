# Security Policy

## Secrets
Never commit `.env`, API keys, database passwords, n8n credential exports, local model access tokens, VINs or personal vehicle records. Public n8n JSON files in this repository have credential references removed.

For a public GitHub repository, enable Secret scanning, Push protection, Dependabot alerts and code scanning where available.

## Vehicle safety
This is a diagnostic-support prototype, not an autonomous repair authority. Safety-critical brake/SRS findings require qualified inspection. Do not bypass safety systems. Clearing DTCs must remain an explicit operator action.

## Reporting
Open a private security advisory or contact the repository maintainer through the project's chosen private channel. Do not post live credentials or personal vehicle data in a public issue.
