# Titre : Intégration d'un analyste IA pour l'aquarium dans ReefControl

**Objectif :**
Ajouter une nouvelle fonctionnalité à l'interface web de ReefControl qui permet à l'utilisateur de demander une analyse de l'état de son aquarium à une IA (ChatGPT). L'utilisateur cliquera sur un bouton, le backend collectera les données, interrogera l'API d'OpenAI, puis affichera la réponse textuelle de l'IA dans l'interface.

**Contexte technique :**
- **Backend :** Python avec Flask (`reef_web.py`, `controller.py`).
- **Frontend :** JavaScript vanilla (`static/js/app.js`) et HTML/Jinja (`templates/index.html`).
- **Dépendances à ajouter :** La librairie `openai` pour Python (`pip install openai`).

---

### Instructions de développement

#### Étape 1 : Interface Utilisateur (Frontend)

**Fichier à modifier : `templates/index.html`**

1.  **Localisation :** Dans l'onglet "Eau" (`<div class="tab-pane fade" id="tab-water" ...>`).
2.  **Ajout de la carte d'analyse :** Après la dernière `reef-card` de cet onglet, ajoutez une nouvelle carte pour l'analyse IA.

    ```html
    <!-- Insérer ce bloc à la fin du div #tab-water -->
    <div class="reef-card mt-3">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div class="fw-semibold">Analyse par Intelligence Artificielle</div>
            <button class="btn btn-primary" data-action="get_ai_analysis">
                <span class="spinner-border spinner-border-sm d-none" role="status" aria-hidden="true" id="aiAnalysisSpinner"></span>
                Lancer l'analyse
            </button>
        </div>
        <div id="aiAnalysisResult" class="small text-secondary">
            Cliquez sur "Lancer l'analyse" pour obtenir un diagnostic et des recommandations de l'IA sur l'état de votre aquarium. La réponse apparaîtra ici.
        </div>
    </div>
    ```

**Fichier à modifier : `static/js/app.js`**

1.  **Nouveau gestionnaire de clic :** Dans l'objet `clickHandlers`, ajoutez une nouvelle fonction pour l'action `get_ai_analysis`.

    ```javascript
    // Dans l'objet clickHandlers
    get_ai_analysis: () => getAiAnalysis(),
    ```

2.  **Nouvelle fonction JavaScript :** Ajoutez la fonction `getAiAnalysis` au fichier. Cette fonction ne passera pas par `apiAction` car la réponse peut être longue et nécessite une gestion asynchrone dédiée.

    ```javascript
    // Ajouter cette nouvelle fonction dans app.js
    async function getAiAnalysis() {
      const resultDiv = document.getElementById("aiAnalysisResult");
      const spinner = document.getElementById("aiAnalysisSpinner");
      const btn = document.querySelector('[data-action="get_ai_analysis"]');

      // Activer le spinner et désactiver le bouton
      spinner.classList.remove("d-none");
      btn.disabled = true;
      resultDiv.innerHTML = "Analyse en cours, veuillez patienter...";

      try {
        const res = await fetch("/api/analyze", {
          method: "POST",
        });

        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.error || `HTTP ${res.status}`);
        }

        const data = await res.json();
        // Utiliser une librairie comme 'marked' serait idéal pour afficher le markdown,
        // mais pour rester simple, on insère le texte brut dans une balise <pre>.
        resultDiv.innerHTML = `<pre style="white-space: pre-wrap; word-wrap: break-word;">${data.analysis}</pre>`;

      } catch (err) {
        console.error("AI Analysis Error:", err);
        resultDiv.innerHTML = `<div class="alert alert-danger">Erreur lors de l'analyse : ${err.message}</div>`;
      } finally {
        // Cacher le spinner et réactiver le bouton
        spinner.classList.add("d-none");
        btn.disabled = false;
      }
    }
    ```

#### Étape 2 : Logique Backend

**Fichier à modifier : `reef_web.py`**

1.  **Importations :** Assurez-vous que `jsonify` est importé depuis `flask`.
2.  **Nouvelle route API :** Ajoutez une nouvelle route dédiée pour l'analyse. Cette approche est plus propre qu'une action générique car elle peut être asynchrone et gérer des traitements longs.

    ```python
    # Ajouter cette nouvelle route à la fin de reef_web.py
    @app.post("/api/analyze")
    def api_analyze():
        try:
            # Note: La fonction du contrôleur est synchrone pour l'instant.
            # Idéalement, elle serait asynchrone avec une librairie adaptée (ex: Quart).
            # Pour Flask, nous l'appelons de manière bloquante.
            analysis_response = controller.get_ai_analysis()
            return jsonify({"analysis": analysis_response})
        except Exception as exc:
            # Logguez l'erreur côté serveur si possible
            return jsonify({"ok": False, "error": str(exc)}), 500
    ```

**Fichier à modifier : `controller.py`**

1.  **Importations :** Ajoutez les importations nécessaires en haut du fichier.

    ```python
    import os
    import openai
    ```

2.  **Nouvelle méthode dans `ReefController` :** Ajoutez la méthode `get_ai_analysis`.

    ```python
    # Ajouter cette méthode à la classe ReefController
    def get_ai_analysis(self) -> str:
        """
        Collecte les données de l'aquarium, interroge l'API d'OpenAI et renvoie l'analyse.
        """
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("La variable d'environnement OPENAI_API_KEY n'est pas définie.")

        client = openai.OpenAI(api_key=api_key)

        # 1. Collecter les données
        # Nous utilisons la méthode existante qui prépare déjà beaucoup de données.
        current_data = self._build_values_payload()

        # 2. Construire le prompt
        prompt_template = """
        Rôle: Tu es un expert en aquariophilie récifale, spécialisé dans l'analyse des paramètres de l'eau et la maintenance des écosystèmes marins.

        Contexte: Voici les données de mon aquarium récifal. Analyse-les et fournis des recommandations claires et actionnables.

        Données:
        ```json
        {data_json}
        ```

        Tâche:
        1.  **Analyse générale**: Sur la base de toutes les données, y a-t-il des paramètres qui sortent des plages idéales pour un aquarium récifal ? Lesquels et pourquoi ?
        2.  **Identification des risques**: Détectes-tu des problèmes potentiels ou des tendances inquiétantes (par exemple, une instabilité, une augmentation des nitrates) ?
        3.  **Santé globale**: Fournis un résumé de l'état de santé général de l'aquarium (Excellent, Bon, Passable, Problématique).
        4.  **Plan d'action**: Propose une liste de recommandations concrètes et priorisées. Pour chaque point, explique la raison en te basant sur les données.

        Format de la réponse: Structure ta réponse avec les sections suivantes :
        -   **Résumé de l'état de santé**
        -   **Points de vigilance**
        -   **Recommandations**
        """
        
        # Convertir les données en JSON pour le prompt
        data_as_json_string = json.dumps(current_data, indent=2)
        final_prompt = prompt_template.format(data_json=data_as_json_string)

        # 3. Appeler l'API d'OpenAI
        try:
            completion = client.chat.completions.create(
                model="gpt-4o-mini", # Un modèle rapide et économique pour commencer
                messages=[
                    {"role": "system", "content": "Tu es un expert en aquariophilie récifale."},
                    {"role": "user", "content": final_prompt}
                ],
                temperature=0.5,
            )
            
            response_content = completion.choices[0].message.content
            if not response_content:
                return "L'IA n'a pas retourné de réponse."
            
            return response_content

        except Exception as e:
            logger.error(f"Erreur lors de l'appel à l'API OpenAI: {e}")
            raise RuntimeError(f"Erreur de communication avec l'API OpenAI: {e}")

    ```

**Note finale :**
Pour que cela fonctionne, l'utilisateur devra définir la variable d'environnement `OPENAI_API_KEY` avec sa clé API OpenAI avant de lancer l'application.
Exemple : `export OPENAI_API_KEY='sk-...'` (sur Linux/macOS) ou `set OPENAI_API_KEY=sk-...` (sur Windows).
