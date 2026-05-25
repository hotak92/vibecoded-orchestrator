/**
 * Excalidraw element reference and color palettes for the read_me tool.
 * Gives the LLM a quick reference for building diagrams without
 * needing to discover conventions through trial and error.
 */
export declare const ELEMENT_REFERENCE: {
    types: {
        rectangle: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
            defaults: {
                width: number;
                height: number;
            };
        };
        ellipse: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
            defaults: {
                width: number;
                height: number;
            };
        };
        diamond: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
            defaults: {
                width: number;
                height: number;
            };
        };
        arrow: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
            example: {
                x: number;
                y: number;
                points: {
                    x: number;
                    y: number;
                }[];
            };
        };
        line: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
        };
        text: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
            defaults: {
                fontSize: number;
            };
        };
        freedraw: {
            description: string;
            requiredFields: string[];
            optionalFields: string[];
        };
    };
    colorPalettes: {
        excalidraw: {
            description: string;
            colors: {
                blue: string;
                red: string;
                green: string;
                orange: string;
                yellow: string;
                purple: string;
                pink: string;
                gray: string;
                black: string;
                white: string;
            };
        };
        pastel: {
            description: string;
            colors: {
                lightBlue: string;
                lightRed: string;
                lightGreen: string;
                lightOrange: string;
                lightYellow: string;
                lightPurple: string;
                lightPink: string;
                lightGray: string;
            };
        };
    };
    sizing: {
        spacing: string;
        textInBox: string;
        arrowGap: string;
        minWidth: string;
        fontSize: {
            title: number;
            heading: number;
            body: number;
            caption: number;
        };
    };
    tips: string[];
};
export declare function getCheatsheetText(): string;
//# sourceMappingURL=cheatsheet.d.ts.map