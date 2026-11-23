# castel-home-products

Home products for Claude Code, in Hebrew. Photograph an invoice, warranty certificate or instruction page, and the skill files the product in your Google Drive, finds the manufacturer's manual, and computes warranty dates with a script (including split warranties). When something breaks, it answers from the product's own manual and the web, labels the source of every step, warns about safety risks, and says whether to use the warranty or weigh repair against replacement.

- Requires Python 3.8+ and the Google Drive connector in claude.ai. Gmail connector optional; web search recommended.
- No API keys. On first run it checks what is missing, explains, and installs or creates folders only with your approval.
- Skill: `home-products` (stages: setup → route → intake / troubleshooting / question → close).

Install instructions: see the repository README.
