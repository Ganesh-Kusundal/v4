.PHONY: graph graph-hook graph-hook-status graph-hook-uninstall

# graphify integration (see CLAUDE.md "Graph refresh & post-commit hook").
# `graphify` is a standalone CLI; the graph lives in graphify-out/.
graph:
	graphify update .

graph-hook:
	graphify hook install

graph-hook-status:
	graphify hook status

graph-hook-uninstall:
	graphify hook uninstall
