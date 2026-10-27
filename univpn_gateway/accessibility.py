"""AT-SPI adapter. Import lazily so normal CLI/tests don't need a desktop stack."""
import re


class AccessibleUI:
    def __init__(self):
        import pyatspi
        self.api = pyatspi

    def nodes(self):
        result = []

        def visit(node, depth=0):
            if depth > 25:
                return
            try:
                if node.getState().contains(self.api.STATE_DEFUNCT):
                    return
                result.append(node)
                for child in node:
                    visit(child, depth + 1)
            except Exception:
                return

        desktop = self.api.Registry.getDesktop(0)
        for app in desktop:
            if "univpn" in app.name.lower():
                visit(app)
        return result

    def find(self, selector):
        nodes = [n for n in self.nodes()
                 if n.getRoleName() == selector["role"]
                 and n.name == selector.get("name", "")
                 and n.getState().contains(self.api.STATE_SHOWING)]
        if len(nodes) != 1:
            raise ValueError(f"selector must match one visible widget; matched {len(nodes)}")
        return nodes[0]

    def fill(self, selector, value):
        node = self.find(selector)
        if not node.queryEditableText().setTextContents(value):
            raise RuntimeError("widget rejected text")

    def press(self, selector):
        node = self.find(selector)
        action = node.queryAction()
        choices = [i for i in range(action.nActions)
                   if action.getName(i).lower() in ("press", "click", "activate")]
        if len(choices) != 1 or not action.doAction(choices[0]):
            raise RuntimeError("widget has no unique activation action")

    def inspect(self):
        # Do not read Text/EditText content; names may still be user identifiers.
        # This output stays local and is explicitly requested by the operator.
        return [{"role": n.getRoleName(), "name": n.name}
                for n in self.nodes() if n.getState().contains(self.api.STATE_SHOWING)]

    def matches(self, selector):
        try:
            self.find(selector)
            return True
        except ValueError:
            return False
